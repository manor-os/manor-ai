from __future__ import annotations

import argparse
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

from pypdf import PdfReader

try:
    from .convert_pdf_to_images import convert
    from .fill_fillable_fields import (
        _ambiguous_widget_names,
        _appearance_is_nonempty,
        _has_digital_signature,
        _resolve,
        _widgets,
    )
except ImportError:  # Direct script execution.
    from convert_pdf_to_images import convert  # type: ignore[no-redef]
    from fill_fillable_fields import (  # type: ignore[no-redef]
        _ambiguous_widget_names,
        _appearance_is_nonempty,
        _has_digital_signature,
        _resolve,
        _widgets,
    )


def _run_pdfinfo(path: Path, password: str | None) -> str:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        raise RuntimeError(
            "Required Poppler command `pdfinfo` was not found. Install poppler-utils before verifying PDFs."
        )
    command = [pdfinfo]
    if password:
        command.extend(["-upw", password])
    command.append(str(path))
    completed = subprocess.run(command, capture_output=True, text=True, check=False)
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or "unknown Poppler error"
        raise RuntimeError(f"pdfinfo failed: {detail}")
    return completed.stdout


def verify_pdf(
    pdf_path: str | Path,
    render_dir: str | Path | None = None,
    *,
    dpi: int = 150,
    password: str | None = None,
    expect_flattened: bool = False,
    expect_interactive: bool = False,
    require_form_appearances: bool = False,
    run_external_checks: bool = True,
) -> dict[str, Any]:
    path = Path(pdf_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"PDF not found: {path}")
    if expect_flattened and expect_interactive:
        raise ValueError("A PDF cannot be expected to be both flattened and interactive")

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        if not password or reader.decrypt(password) == 0:
            raise ValueError("The PDF is encrypted; provide a valid password to verify it")
    page_count = len(reader.pages)
    if page_count < 1:
        raise ValueError("The PDF has no pages")

    canonical_field_count = len(reader.get_fields() or {})
    widgets = _widgets(reader)
    root = _resolve(reader.trailer.get("/Root"))
    has_acroform = bool(root and root.get("/AcroForm") is not None)
    missing_appearance = [
        {"name": widget["name"], "page": widget["page"]}
        for widget in widgets
        if not _appearance_is_nonempty(widget["normal_appearance"])
    ]
    ambiguous = _ambiguous_widget_names(reader)
    signatures_present = _has_digital_signature(reader)
    errors: list[str] = []
    warnings: list[str] = []

    if ambiguous:
        errors.append(f"Ambiguous duplicate field/widget names: {ambiguous}")
    if expect_flattened:
        if widgets:
            errors.append(f"Expected a flattened PDF but found {len(widgets)} /Widget annotation(s)")
        if has_acroform:
            errors.append("Expected a flattened PDF but found an /AcroForm tree")
    if expect_interactive:
        if not canonical_field_count:
            errors.append("Expected an interactive PDF but found no canonical AcroForm fields")
        if not widgets:
            errors.append("Expected an interactive PDF but found no page widgets")
    if require_form_appearances and missing_appearance:
        errors.append(f"Form widgets have missing or empty /AP /N appearances: {missing_appearance}")
    elif missing_appearance:
        warnings.append(f"{len(missing_appearance)} form widget(s) have missing or empty appearances")
    if signatures_present:
        warnings.append("A digital signature is present; any subsequent edit will invalidate it")

    rendered: list[dict[str, object]] = []
    pdfinfo_output = ""
    if run_external_checks:
        pdfinfo_output = _run_pdfinfo(path, password)
        destination = (
            Path(render_dir).expanduser().resolve() if render_dir is not None else path.parent / f"{path.stem}-rendered"
        )
        rendered = convert(path, destination, dpi=dpi, prefix="page", password=password)
        if len(rendered) != page_count:
            errors.append(f"Rendered {len(rendered)} page(s), but the PDF contains {page_count}")

    return {
        "path": str(path),
        "bytes": path.stat().st_size,
        "page_count": page_count,
        "encrypted": reader.is_encrypted,
        "canonical_field_count": canonical_field_count,
        "widget_count": len(widgets),
        "has_acroform": has_acroform,
        "missing_appearance_widgets": missing_appearance,
        "ambiguous_widget_names": ambiguous,
        "signatures_present": signatures_present,
        "pdfinfo_checked": bool(pdfinfo_output),
        "rendered_pages": rendered,
        "machine_checks_passed": not errors,
        "visual_review_required": True,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Reopen, inspect, and render a PDF before visual delivery review")
    parser.add_argument("input_pdf")
    parser.add_argument("render_directory")
    parser.add_argument("--dpi", type=int, default=150)
    parser.add_argument("--password", help="User password for an encrypted PDF")
    expectation = parser.add_mutually_exclusive_group()
    expectation.add_argument("--expect-flattened", action="store_true")
    expectation.add_argument("--expect-interactive", action="store_true")
    parser.add_argument("--require-form-appearances", action="store_true")
    args = parser.parse_args()
    result = verify_pdf(
        args.input_pdf,
        args.render_directory,
        dpi=args.dpi,
        password=args.password,
        expect_flattened=args.expect_flattened,
        expect_interactive=args.expect_interactive,
        require_form_appearances=args.require_form_appearances,
    )
    print(json.dumps(result, ensure_ascii=False))
    if not result["machine_checks_passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
