from __future__ import annotations

import json
import shutil
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

from packages.core.ai.skills.pdf.scripts.check_fillable_fields import inspect_fillable_fields
from packages.core.ai.skills.pdf.scripts.convert_pdf_to_images import convert
from packages.core.ai.skills.pdf.scripts.extract_form_field_info import get_field_info
from packages.core.ai.skills.pdf.scripts.fill_fillable_fields import fill_pdf_fields
from packages.core.ai.skills.pdf.scripts.fill_pdf_form_with_annotations import fill_pdf_form
from packages.core.ai.skills.pdf.scripts.verify_pdf import verify_pdf


def _text_widget(name: str = "full_name") -> DictionaryObject:
    return DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Annot"),
            NameObject("/Subtype"): NameObject("/Widget"),
            NameObject("/FT"): NameObject("/Tx"),
            NameObject("/T"): TextStringObject(name),
            NameObject("/V"): TextStringObject(""),
            NameObject("/Rect"): RectangleObject([72, 700, 300, 730]),
            NameObject("/F"): NumberObject(4),
            NameObject("/DA"): TextStringObject("/Helv 12 Tf 0 g"),
        }
    )


def _write_text_form(path: Path, *, attach_to_acroform: bool = True) -> None:
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
    field_ref = writer._add_object(_text_widget())
    page[NameObject("/Annots")] = ArrayObject([field_ref])
    if attach_to_acroform:
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


def test_field_diagnostics_find_orphan_page_widgets(tmp_path: Path) -> None:
    source = tmp_path / "orphan.pdf"
    _write_text_form(source, attach_to_acroform=False)

    diagnostics = inspect_fillable_fields(source)
    extracted = get_field_info(PdfReader(source))

    assert diagnostics["fillable"] is True
    assert diagnostics["canonical_field_count"] == 0
    assert diagnostics["widget_count"] == 1
    assert diagnostics["orphan_widget_names"] == ["full_name"]
    assert extracted == [
        {
            "field_id": "full_name",
            "page": 1,
            "rect": [72, 700, 300, 730],
            "type": "text",
        }
    ]


def test_verify_pdf_checks_interactive_appearances_without_external_tools(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    values = tmp_path / "values.json"
    output = tmp_path / "filled.pdf"
    _write_text_form(source)
    values.write_text(
        json.dumps([{"field_id": "full_name", "page": 1, "value": "Ada Lovelace"}]),
        encoding="utf-8",
    )
    fill_pdf_fields(str(source), str(values), str(output))

    report = verify_pdf(
        output,
        expect_interactive=True,
        require_form_appearances=True,
        run_external_checks=False,
    )

    assert report["machine_checks_passed"] is True
    assert report["canonical_field_count"] == 1
    assert report["widget_count"] == 1
    assert report["missing_appearance_widgets"] == []
    assert report["visual_review_required"] is True


def test_annotation_fill_preserves_source_and_reopens_output(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    fields = tmp_path / "fields.json"
    output = tmp_path / "output" / "annotated.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as stream:
        writer.write(stream)
    source_bytes = source.read_bytes()
    fields.write_text(
        json.dumps(
            {
                "pages": [{"page_number": 1, "pdf_width": 612, "pdf_height": 792}],
                "form_fields": [
                    {
                        "page_number": 1,
                        "entry_bounding_box": [72, 90, 260, 120],
                        "entry_text": {"text": "Ada Lovelace", "font_size": 12},
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = fill_pdf_form(str(source), str(fields), str(output))

    assert result["validated"] is True
    assert result["annotations_added"] == 1
    assert source.read_bytes() == source_bytes
    annotation = PdfReader(output).pages[0]["/Annots"][0].get_object()
    assert annotation["/Subtype"] == "/FreeText"
    assert annotation["/Contents"] == "Ada Lovelace"


def test_annotation_fill_refuses_source_overwrite(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    fields = tmp_path / "fields.json"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as stream:
        writer.write(stream)
    fields.write_text(json.dumps({"pages": [], "form_fields": []}), encoding="utf-8")

    with pytest.raises(ValueError, match="Input and output paths must differ"):
        fill_pdf_form(str(source), str(fields), str(source))


@pytest.mark.skipif(
    shutil.which("pdftoppm") is None or shutil.which("pdfinfo") is None,
    reason="Poppler is not installed",
)
def test_poppler_render_and_delivery_gate_cover_every_page(tmp_path: Path) -> None:
    source = tmp_path / "two-pages.pdf"
    writer = PdfWriter()
    writer.add_blank_page(width=612, height=792)
    writer.add_blank_page(width=612, height=792)
    with source.open("wb") as stream:
        writer.write(stream)

    manifest = convert(source, tmp_path / "rendered", dpi=72)
    report = verify_pdf(source, tmp_path / "verified", dpi=72)

    assert [image["page"] for image in manifest] == [1, 2]
    assert [(image["width"], image["height"]) for image in manifest] == [(612, 792), (612, 792)]
    assert report["machine_checks_passed"] is True
    assert report["pdfinfo_checked"] is True
    assert len(report["rendered_pages"]) == 2
