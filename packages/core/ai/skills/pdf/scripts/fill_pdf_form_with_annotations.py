from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.annotations import FreeText

try:
    from .fill_fillable_fields import _has_digital_signature, _resolve
except ImportError:  # Direct script execution.
    from fill_fillable_fields import _has_digital_signature, _resolve  # type: ignore[no-redef]


def transform_from_image_coords(
    bbox: list[float],
    image_width: float,
    image_height: float,
    pdf_width: float,
    pdf_height: float,
) -> tuple[float, float, float, float]:
    if image_width <= 0 or image_height <= 0:
        raise ValueError("Image dimensions must be greater than zero")
    x_scale = pdf_width / image_width
    y_scale = pdf_height / image_height
    left = bbox[0] * x_scale
    right = bbox[2] * x_scale
    top = pdf_height - (bbox[1] * y_scale)
    bottom = pdf_height - (bbox[3] * y_scale)
    return left, bottom, right, top


def transform_from_pdf_coords(bbox: list[float], pdf_height: float) -> tuple[float, float, float, float]:
    left = bbox[0]
    right = bbox[2]
    top = pdf_height - bbox[1]
    bottom = pdf_height - bbox[3]
    return left, bottom, right, top


def _bounding_box(value: Any, *, field_index: int) -> list[float]:
    if not isinstance(value, list) or len(value) != 4:
        raise ValueError(f"form_fields[{field_index}].entry_bounding_box must contain four numbers")
    try:
        result = [float(coordinate) for coordinate in value]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"form_fields[{field_index}].entry_bounding_box must contain four numbers") from exc
    if result[0] >= result[2] or result[1] >= result[3]:
        raise ValueError(f"form_fields[{field_index}].entry_bounding_box is empty or inverted")
    return result


def _count_free_text_annotations(reader: PdfReader) -> int:
    return sum(
        1
        for page in reader.pages
        for raw_annotation in (page.get("/Annots", ()) or ())
        if _resolve(raw_annotation).get("/Subtype") == "/FreeText"
    )


def fill_pdf_form(
    input_pdf_path: str,
    fields_json_path: str,
    output_pdf_path: str,
    *,
    allow_invalidating_signatures: bool = False,
) -> dict[str, Any]:
    input_path = Path(input_pdf_path).expanduser().resolve()
    output_path = Path(output_pdf_path).expanduser().resolve()
    if input_path == output_path:
        raise ValueError("Input and output paths must differ; preserve the source PDF")

    fields_data = json.loads(Path(fields_json_path).read_text(encoding="utf-8"))
    if not isinstance(fields_data, dict):
        raise ValueError("fields.json must contain a JSON object")
    pages_data = fields_data.get("pages")
    form_fields = fields_data.get("form_fields")
    if not isinstance(pages_data, list) or not isinstance(form_fields, list):
        raise ValueError("fields.json requires `pages` and `form_fields` arrays")
    page_info_by_number = {
        int(page["page_number"]): page for page in pages_data if isinstance(page, dict) and "page_number" in page
    }

    reader = PdfReader(str(input_path), strict=False)
    if _has_digital_signature(reader) and not allow_invalidating_signatures:
        raise ValueError(
            "The source PDF contains a digital signature. Editing would invalidate it; "
            "rerun only after explicit user approval with --allow-invalidating-signatures."
        )
    original_page_count = len(reader.pages)
    original_annotation_count = _count_free_text_annotations(reader)
    writer = PdfWriter()
    writer.clone_document_from_reader(reader)

    pdf_dimensions = {
        index: (float(page.mediabox.width), float(page.mediabox.height)) for index, page in enumerate(reader.pages, 1)
    }
    annotations_added = 0
    for field_index, field in enumerate(form_fields):
        if not isinstance(field, dict):
            raise ValueError(f"form_fields[{field_index}] must be an object")
        try:
            page_number = int(field["page_number"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"form_fields[{field_index}].page_number must be an integer") from exc
        if page_number not in pdf_dimensions:
            raise ValueError(f"form_fields[{field_index}].page_number {page_number} is outside the PDF page range")
        page_info = page_info_by_number.get(page_number)
        if page_info is None:
            raise ValueError(f"Missing pages entry for page {page_number}")

        entry_text = field.get("entry_text")
        if not isinstance(entry_text, dict) or not entry_text.get("text"):
            continue
        bbox = _bounding_box(field.get("entry_bounding_box"), field_index=field_index)
        pdf_width, pdf_height = pdf_dimensions[page_number]
        if "pdf_width" in page_info:
            transformed_box = transform_from_pdf_coords(bbox, pdf_height)
        else:
            try:
                image_width = float(page_info["image_width"])
                image_height = float(page_info["image_height"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError(f"Page {page_number} needs pdf_width/pdf_height or image_width/image_height") from exc
            transformed_box = transform_from_image_coords(
                bbox,
                image_width,
                image_height,
                pdf_width,
                pdf_height,
            )

        annotation = FreeText(
            text=str(entry_text["text"]),
            rect=transformed_box,
            font=str(entry_text.get("font", "Helvetica")),
            font_size=f"{float(entry_text.get('font_size', 14)):g}pt",
            font_color=str(entry_text.get("font_color", "000000")).lstrip("#"),
            border_color=None,
            background_color=None,
        )
        writer.add_annotation(page_number=page_number - 1, annotation=annotation)
        annotations_added += 1

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}-",
            suffix=".tmp.pdf",
            dir=output_path.parent,
            delete=False,
        ) as stream:
            temporary_path = Path(stream.name)
            writer.write(stream)
        reopened = PdfReader(str(temporary_path), strict=False)
        if len(reopened.pages) != original_page_count:
            raise ValueError("Page count changed while adding text annotations")
        actual_annotations = _count_free_text_annotations(reopened) - original_annotation_count
        if actual_annotations != annotations_added:
            raise ValueError(f"Expected {annotations_added} new FreeText annotation(s), found {actual_annotations}")
        os.replace(temporary_path, output_path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)

    return {
        "output": str(output_path),
        "annotations_added": annotations_added,
        "page_count": original_page_count,
        "validated": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Add validated FreeText annotations to a non-fillable PDF")
    parser.add_argument("input_pdf")
    parser.add_argument("fields_json")
    parser.add_argument("output_pdf")
    parser.add_argument(
        "--allow-invalidating-signatures",
        action="store_true",
        help="Proceed only after the user explicitly accepts invalidating digital signatures",
    )
    args = parser.parse_args()
    result = fill_pdf_form(
        args.input_pdf,
        args.fields_json,
        args.output_pdf,
        allow_invalidating_signatures=args.allow_invalidating_signatures,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
