from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from pypdf import PdfReader

try:
    from .fill_fillable_fields import (
        _ambiguous_widget_names,
        _has_digital_signature,
        _widgets,
    )
except ImportError:  # Direct script execution.
    from fill_fillable_fields import (  # type: ignore[no-redef]
        _ambiguous_widget_names,
        _has_digital_signature,
        _widgets,
    )


def inspect_fillable_fields(pdf_path: str | Path) -> dict[str, Any]:
    path = Path(pdf_path).expanduser().resolve()
    reader = PdfReader(str(path), strict=False)
    canonical_names = sorted((reader.get_fields() or {}).keys())
    widgets = _widgets(reader)
    widget_names = sorted({str(widget["name"]) for widget in widgets if widget["name"]})
    orphan_widget_names = sorted(set(widget_names) - set(canonical_names))
    unnamed_widgets = sum(1 for widget in widgets if not widget["name"])
    ambiguous_names = _ambiguous_widget_names(reader)
    signatures_present = _has_digital_signature(reader)
    warnings: list[str] = []
    if orphan_widget_names:
        warnings.append(
            "Page widgets are missing from the canonical /AcroForm tree and require controlled reattachment"
        )
    if unnamed_widgets:
        warnings.append("Unnamed page widgets cannot be filled safely")
    if ambiguous_names:
        warnings.append("Duplicate names refer to unrelated canonical fields and page widgets")
    if signatures_present:
        warnings.append("Editing this PDF would invalidate a digital signature")
    return {
        "path": str(path),
        "page_count": len(reader.pages),
        "fillable": bool(canonical_names or widget_names),
        "canonical_field_count": len(canonical_names),
        "canonical_field_names": canonical_names,
        "widget_count": len(widgets),
        "widget_names": widget_names,
        "orphan_widget_names": orphan_widget_names,
        "unnamed_widget_count": unnamed_widgets,
        "ambiguous_widget_names": ambiguous_names,
        "signatures_present": signatures_present,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect both the canonical AcroForm tree and visible page widgets")
    parser.add_argument("input_pdf")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = inspect_fillable_fields(args.input_pdf)
    if args.json:
        print(json.dumps(result, ensure_ascii=False))
        return

    if result["fillable"]:
        print("This PDF has fillable form fields")
    else:
        print("This PDF does not have fillable form fields; use structure extraction or visual placement")
    print(
        "Canonical fields: "
        f"{result['canonical_field_count']}; page widgets: {result['widget_count']}; "
        f"orphan widget names: {len(result['orphan_widget_names'])}"
    )
    for warning in result["warnings"]:
        print(f"WARNING: {warning}")


if __name__ == "__main__":
    main()
