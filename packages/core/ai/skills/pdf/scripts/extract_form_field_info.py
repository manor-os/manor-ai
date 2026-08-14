from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from pypdf import PdfReader

try:
    from .fill_fillable_fields import _field_name, _inherited_value, _resolve
except ImportError:  # Direct script execution.
    from fill_fillable_fields import _field_name, _inherited_value, _resolve  # type: ignore[no-redef]


def _plain(value: Any) -> Any:
    resolved = _resolve(value)
    if isinstance(resolved, (list, tuple)):
        return [_plain(item) for item in resolved]
    if isinstance(resolved, dict):
        return {str(key): _plain(item) for key, item in resolved.items()}
    if resolved is None or isinstance(resolved, (str, int, float, bool)):
        return resolved
    return str(resolved)


def _button_states(widget: Any) -> list[str]:
    annotation = _resolve(widget)
    appearance = _resolve(annotation.get("/AP")) if annotation.get("/AP") else None
    normal = _resolve(appearance.get("/N")) if hasattr(appearance, "get") and appearance.get("/N") else None
    return sorted(str(key) for key in normal) if isinstance(normal, dict) else []


def _choice_options(field: Any) -> list[dict[str, str]]:
    options: list[dict[str, str]] = []
    for option in _inherited_value(field, "/Opt") or ():
        resolved = _resolve(option)
        if isinstance(resolved, (list, tuple)) and resolved:
            value = str(resolved[0])
            text = str(resolved[1] if len(resolved) > 1 else resolved[0])
        else:
            value = text = str(resolved)
        options.append({"value": value, "text": text})
    return options


def get_field_info(reader: PdfReader) -> list[dict[str, Any]]:
    canonical_fields = reader.get_fields() or {}
    widgets_by_name: dict[str, list[dict[str, Any]]] = {}
    unnamed_widgets = 0
    for page_number, page in enumerate(reader.pages, 1):
        for raw_annotation in page.get("/Annots", ()) or ():
            annotation = _resolve(raw_annotation)
            if annotation.get("/Subtype") != "/Widget":
                continue
            name = _field_name(raw_annotation)
            if not name:
                unnamed_widgets += 1
                continue
            widgets_by_name.setdefault(name, []).append(
                {
                    "raw": raw_annotation,
                    "page": page_number,
                    "rect": _plain(annotation.get("/Rect")),
                    "states": _button_states(raw_annotation),
                }
            )

    if unnamed_widgets:
        print(f"WARNING: ignored {unnamed_widgets} unnamed widget(s)", file=sys.stderr)

    field_names = sorted(set(canonical_fields) | set(widgets_by_name))
    result: list[dict[str, Any]] = []
    for field_id in field_names:
        locations = widgets_by_name.get(field_id, [])
        field = canonical_fields.get(field_id) or (locations[0]["raw"] if locations else None)
        if field is None:
            continue
        field_type = str(_inherited_value(field, "/FT") or "")
        flags = int(_inherited_value(field, "/Ff") or 0)
        item: dict[str, Any] = {"field_id": field_id}
        if locations:
            item["page"] = locations[0]["page"]
            item["rect"] = locations[0]["rect"]
            if len(locations) > 1:
                item["widgets"] = [{"page": location["page"], "rect": location["rect"]} for location in locations]
        else:
            item["warning"] = "Canonical field has no visible page widget"

        if field_type == "/Tx":
            item["type"] = "text"
        elif field_type == "/Btn" and flags & (1 << 15):
            item["type"] = "radio_group"
            options: list[dict[str, Any]] = []
            for location in locations:
                for state in location["states"]:
                    if state != "/Off":
                        options.append({"value": state, "page": location["page"], "rect": location["rect"]})
            item["radio_options"] = options
        elif field_type == "/Btn":
            item["type"] = "checkbox"
            allowed = sorted({state for location in locations for state in location["states"] if state != "/Off"})
            item["checked_value"] = allowed[0] if allowed else None
            item["unchecked_value"] = "/Off"
            if len(allowed) > 1:
                item["allowed_checked_values"] = allowed
        elif field_type == "/Ch":
            item["type"] = "choice"
            item["choice_options"] = _choice_options(field)
        elif field_type == "/Sig":
            item["type"] = "signature"
        else:
            item["type"] = f"unknown ({field_type or 'missing /FT'})"
        result.append(item)

    result.sort(
        key=lambda field: (
            int(field.get("page", 10**9)),
            -float((field.get("rect") or [0, 0, 0, 0])[1]),
            float((field.get("rect") or [0, 0, 0, 0])[0]),
            str(field["field_id"]),
        )
    )
    return result


def write_field_info(pdf_path: str, json_output_path: str) -> list[dict[str, Any]]:
    input_path = Path(pdf_path).expanduser().resolve()
    output_path = Path(json_output_path).expanduser().resolve()
    if input_path == output_path:
        raise ValueError("Input PDF and JSON output paths must differ")
    reader = PdfReader(str(input_path), strict=False)
    field_info = get_field_info(reader)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(field_info, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"Wrote {len(field_info)} fields to {output_path}")
    return field_info


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract canonical AcroForm fields and visible page-widget locations")
    parser.add_argument("input_pdf")
    parser.add_argument("output_json")
    args = parser.parse_args()
    write_field_info(args.input_pdf, args.output_json)


if __name__ == "__main__":
    main()
