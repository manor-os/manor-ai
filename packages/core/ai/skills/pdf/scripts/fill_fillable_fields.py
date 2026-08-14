from __future__ import annotations

import argparse
import json
import os
import tempfile
from collections import defaultdict
from pathlib import Path
from typing import Any

from pypdf import PdfReader, PdfWriter
from pypdf.generic import NameObject


def _resolve(value: Any) -> Any:
    try:
        return value.get_object()
    except AttributeError:
        return value


def _reference_key(value: Any) -> tuple[int, int] | None:
    reference = value if hasattr(value, "idnum") else getattr(_resolve(value), "indirect_reference", None)
    if reference is None or not hasattr(reference, "idnum"):
        return None
    return int(reference.idnum), int(getattr(reference, "generation", 0))


def _field_name(value: Any) -> str | None:
    parts: list[str] = []
    seen: set[tuple[int, int] | int] = set()
    current = value
    while current is not None:
        resolved = _resolve(current)
        marker: tuple[int, int] | int = _reference_key(current) or id(resolved)
        if marker in seen:
            break
        seen.add(marker)
        name = resolved.get("/T") if hasattr(resolved, "get") else None
        if name is not None and str(name):
            parts.append(str(name))
        current = resolved.get("/Parent") if hasattr(resolved, "get") else None
    return ".".join(reversed(parts)) if parts else None


def _inherited_value(value: Any, key: str) -> Any:
    seen: set[tuple[int, int] | int] = set()
    current = value
    while current is not None:
        resolved = _resolve(current)
        marker: tuple[int, int] | int = _reference_key(current) or id(resolved)
        if marker in seen:
            break
        seen.add(marker)
        if hasattr(resolved, "get") and resolved.get(key) is not None:
            return resolved.get(key)
        current = resolved.get("/Parent") if hasattr(resolved, "get") else None
    return None


def _parent_reference_keys(value: Any) -> set[tuple[int, int]]:
    keys: set[tuple[int, int]] = set()
    current = _resolve(value).get("/Parent")
    while current is not None:
        key = _reference_key(current)
        if key is not None:
            if key in keys:
                break
            keys.add(key)
        resolved = _resolve(current)
        current = resolved.get("/Parent") if hasattr(resolved, "get") else None
    return keys


def _widgets(reader: PdfReader) -> list[dict[str, Any]]:
    widgets: list[dict[str, Any]] = []
    for page_number, page in enumerate(reader.pages, 1):
        for raw_annotation in page.get("/Annots", ()) or ():
            annotation = _resolve(raw_annotation)
            if annotation.get("/Subtype") != "/Widget":
                continue
            appearance = _resolve(annotation.get("/AP")) if annotation.get("/AP") else None
            normal_appearance = appearance.get("/N") if hasattr(appearance, "get") else None
            widgets.append(
                {
                    "name": _field_name(raw_annotation),
                    "page": page_number,
                    "reference": _reference_key(raw_annotation),
                    "parents": _parent_reference_keys(raw_annotation),
                    "value": _inherited_value(raw_annotation, "/V"),
                    "normal_appearance": normal_appearance,
                }
            )
    return widgets


def _canonical_field_references(reader: PdfReader) -> dict[str, set[tuple[int, int]]]:
    references: dict[str, set[tuple[int, int]]] = defaultdict(set)
    root = _resolve(reader.trailer.get("/Root"))
    acroform = _resolve(root.get("/AcroForm")) if root and root.get("/AcroForm") else None
    top_fields = acroform.get("/Fields", ()) if acroform else ()
    visited: set[tuple[int, int] | int] = set()

    def visit(raw_field: Any) -> None:
        field = _resolve(raw_field)
        marker: tuple[int, int] | int = _reference_key(raw_field) or id(field)
        if marker in visited:
            return
        visited.add(marker)
        name = _field_name(raw_field)
        key = _reference_key(raw_field)
        if name and key is not None:
            references[name].add(key)
        for kid in field.get("/Kids", ()) or ():
            visit(kid)

    for raw_field in top_fields or ():
        visit(raw_field)
    return references


def _ambiguous_widget_names(reader: PdfReader) -> list[str]:
    canonical = _canonical_field_references(reader)
    ambiguous: set[str] = set()
    for widget in _widgets(reader):
        name = widget["name"]
        if not name or name not in canonical:
            continue
        related = set(widget["parents"])
        if widget["reference"] is not None:
            related.add(widget["reference"])
        if canonical[name].isdisjoint(related):
            ambiguous.add(name)
    return sorted(ambiguous)


def _normalized_value(value: Any) -> Any:
    value = _resolve(value)
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return tuple(_normalized_value(item) for item in value)
    return str(value)


def _appearance_is_nonempty(value: Any) -> bool:
    if value is None:
        return False
    resolved = _resolve(value)
    if isinstance(resolved, dict):
        return bool(resolved)
    return True


def _has_digital_signature(reader: PdfReader) -> bool:
    for field in (reader.get_fields() or {}).values():
        if _inherited_value(field, "/FT") == "/Sig":
            return True
        value = _resolve(field.get("/V")) if field.get("/V") else None
        if hasattr(value, "get") and value.get("/ByteRange") is not None:
            return True
    return any(_inherited_value(widget, "/FT") == "/Sig" for page in reader.pages for widget in (page.get("/Annots", ()) or ()))


def _choice_values(field: Any) -> set[str]:
    values: set[str] = set()
    for option in _inherited_value(field, "/Opt") or ():
        resolved = _resolve(option)
        if isinstance(resolved, (list, tuple)) and resolved:
            values.add(str(resolved[0]))
        else:
            values.add(str(resolved))
    return values


def _button_values(name: str, reader: PdfReader) -> set[str]:
    values = {"/Off"}
    for page in reader.pages:
        for raw_annotation in page.get("/Annots", ()) or ():
            if _field_name(raw_annotation) != name:
                continue
            annotation = _resolve(raw_annotation)
            appearance = _resolve(annotation.get("/AP")) if annotation.get("/AP") else None
            normal = _resolve(appearance.get("/N")) if hasattr(appearance, "get") and appearance.get("/N") else None
            if isinstance(normal, dict):
                values.update(str(key) for key in normal.keys())
    return values


def _validate_requested_values(
    reader: PdfReader,
    requested: list[dict[str, Any]],
    *,
    fields: dict[str, Any] | None = None,
) -> None:
    fields = fields if fields is not None else (reader.get_fields() or {})
    widgets = _widgets(reader)
    pages_by_name: dict[str, set[int]] = defaultdict(set)
    for widget in widgets:
        if widget["name"]:
            pages_by_name[widget["name"]].add(widget["page"])

    errors: list[str] = []
    for item in requested:
        field_id = str(item.get("field_id") or "").strip()
        if not field_id:
            errors.append("Every field entry requires a non-empty field_id")
            continue
        field = fields.get(field_id)
        if field is None:
            errors.append(f"`{field_id}` is not a valid canonical field ID")
            continue
        requested_page = item.get("page")
        if requested_page is not None and pages_by_name.get(field_id) and int(requested_page) not in pages_by_name[field_id]:
            errors.append(
                f"Incorrect page number for `{field_id}` (got {requested_page}, expected one of {sorted(pages_by_name[field_id])})"
            )
        if "value" not in item:
            continue
        field_type = str(_inherited_value(field, "/FT") or "")
        requested_value = _normalized_value(item["value"])
        if field_type == "/Btn":
            allowed = _button_values(field_id, reader)
            if allowed and requested_value not in allowed:
                errors.append(
                    f"Invalid value {requested_value!r} for button field `{field_id}`; valid values are {sorted(allowed)}"
                )
        elif field_type == "/Ch":
            allowed = _choice_values(field)
            if allowed and requested_value not in allowed:
                errors.append(
                    f"Invalid value {requested_value!r} for choice field `{field_id}`; valid values are {sorted(allowed)}"
                )
    if errors:
        raise ValueError("\n".join(errors))


def _expected_values(requested: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        str(item["field_id"]): item["value"]
        for item in requested
        if item.get("field_id") and "value" in item
    }


def _validate_interactive_output(path: Path, expected: dict[str, Any]) -> None:
    reader = PdfReader(str(path), strict=False)
    fields = reader.get_fields() or {}
    missing = sorted(set(expected) - set(fields))
    if missing:
        raise ValueError(f"Fields missing after write: {missing}")
    widgets_by_name: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for widget in _widgets(reader):
        if widget["name"]:
            widgets_by_name[widget["name"]].append(widget)

    errors: list[str] = []
    for name, expected_value in expected.items():
        canonical_value = _normalized_value(fields[name].get("/V"))
        normalized_expected = _normalized_value(expected_value)
        if canonical_value != normalized_expected:
            errors.append(
                f"Canonical value mismatch for `{name}`: expected {normalized_expected!r}, got {canonical_value!r}"
            )
        field_widgets = widgets_by_name.get(name, [])
        if not field_widgets:
            errors.append(f"No page widget found for updated field `{name}`")
            continue
        for widget in field_widgets:
            widget_value = _normalized_value(widget["value"])
            if widget_value != normalized_expected:
                errors.append(
                    f"Widget value mismatch for `{name}` on page {widget['page']}: expected {normalized_expected!r}, got {widget_value!r}"
                )
            if not _appearance_is_nonempty(widget["normal_appearance"]):
                errors.append(f"Widget `{name}` on page {widget['page']} has no non-empty /AP /N appearance")
    if errors:
        raise ValueError("\n".join(errors))


def _validate_flattened_output(path: Path) -> None:
    reader = PdfReader(str(path), strict=False)
    widget_count = len(_widgets(reader))
    root = _resolve(reader.trailer.get("/Root"))
    if widget_count:
        raise ValueError(f"Flattened PDF still contains {widget_count} /Widget annotation(s)")
    if root and root.get("/AcroForm") is not None:
        raise ValueError("Flattened PDF still contains an /AcroForm tree")


def fill_pdf_fields(
    input_pdf_path: str,
    fields_json_path: str,
    output_pdf_path: str,
    *,
    flatten: bool = False,
    allow_invalidating_signatures: bool = False,
) -> dict[str, Any]:
    input_path = Path(input_pdf_path).expanduser().resolve()
    output_path = Path(output_pdf_path).expanduser().resolve()
    if input_path == output_path:
        raise ValueError("Input and output paths must differ; preserve the source PDF")

    requested = json.loads(Path(fields_json_path).read_text(encoding="utf-8"))
    if not isinstance(requested, list):
        raise ValueError("field_values.json must contain a JSON array")

    reader = PdfReader(str(input_path), strict=False)
    if _has_digital_signature(reader) and not allow_invalidating_signatures:
        raise ValueError(
            "The source PDF contains a digital signature. Editing would invalidate it; "
            "preserve the signed original or rerun only after explicit user approval with "
            "--allow-invalidating-signatures."
        )
    ambiguous = _ambiguous_widget_names(reader)
    if ambiguous:
        raise ValueError(
            "Ambiguous duplicate field/widget objects found for: "
            f"{ambiguous}. Refusing to reattach or fill them interactively."
        )

    writer = PdfWriter()
    writer.clone_document_from_reader(reader)
    initial_fields = writer.get_fields() or {}
    widget_names = {widget["name"] for widget in _widgets(reader) if widget["name"]}
    expected = _expected_values(requested)
    if (widget_names - set(initial_fields)) or (set(expected) - set(initial_fields)):
        writer.reattach_fields()

    repaired_reader_fields = writer.get_fields() or {}
    missing = sorted(set(expected) - set(repaired_reader_fields))
    if missing:
        raise ValueError(f"Form fields not found after orphan-widget repair: {missing}")

    # Validate against the repaired writer by first checking the source-facing field
    # contract. Values and pages are checked again after the output is reopened.
    _validate_requested_values(reader, requested, fields=repaired_reader_fields)

    values_to_write = dict(expected)
    if flatten:
        values_to_write = {
            name: field.get("/V", "/Off" if _inherited_value(field, "/FT") == "/Btn" else "")
            for name, field in repaired_reader_fields.items()
        }
        values_to_write.update(expected)

    writer.update_page_form_field_values(
        None,
        values_to_write,
        auto_regenerate=False,
        flatten=flatten,
    )
    if flatten:
        writer.remove_annotations(subtypes="/Widget")
        writer.root_object.pop(NameObject("/AcroForm"), None)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            prefix=f".{output_path.stem}-",
            suffix=".tmp.pdf",
            dir=output_path.parent,
            delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            writer.write(stream)
        if flatten:
            _validate_flattened_output(temp_path)
        else:
            _validate_interactive_output(temp_path, expected)
        os.replace(temp_path, output_path)
        temp_path = None
    finally:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    return {
        "output": str(output_path),
        "fields_updated": len(expected),
        "flattened": flatten,
        "validated": True,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Fill and validate an AcroForm PDF")
    parser.add_argument("input_pdf")
    parser.add_argument("field_values_json")
    parser.add_argument("output_pdf")
    parser.add_argument(
        "--flatten",
        action="store_true",
        help="Create a static copy and remove all widgets and the AcroForm tree",
    )
    parser.add_argument(
        "--allow-invalidating-signatures",
        action="store_true",
        help="Proceed only after the user explicitly accepts invalidating digital signatures",
    )
    args = parser.parse_args()
    result = fill_pdf_fields(
        args.input_pdf,
        args.field_values_json,
        args.output_pdf,
        flatten=args.flatten,
        allow_invalidating_signatures=args.allow_invalidating_signatures,
    )
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
