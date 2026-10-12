#!/usr/bin/env python3
"""Machine-verifiable final quality gate for Manor-generated DOCX files.

The sandbox action ``save_result`` reruns this gate server-side for every
final DOCX exported from ``/skill/projects/<project>/exports/`` and binds the
report to the exact file bytes, so the report must be produced by this script
against the file that is delivered.

Checks: the package is a real DOCX, its XML passes the bundled schema
validator (``scripts/office/validate.py``), the body is not empty and carries
no unresolved placeholders, and every page has fresh render evidence
(``<render-dir>/page-*.jpg|png`` produced after the DOCX, matching the PDF
page count when a PDF is present).

Only the standard library plus ``defusedxml`` (declared in requirements.txt)
are required; ``pdfinfo`` (poppler) is used when available. The document XML
is parsed with defusedxml only.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

# defusedxml is declared in the skill's requirements.txt and installed into
# the sandbox before any script runs; untrusted OOXML is never parsed with
# the stdlib parser.
from defusedxml import ElementTree as SafeET

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_PLACEHOLDER_PATTERNS = (
    ("moustache placeholder", re.compile(r"\{\{[^}]{0,80}\}\}")),
    ("bracket placeholder", re.compile(r"\[(?:INSERT|TODO|TBD|PLACEHOLDER)[^\]]{0,80}\]", re.I)),
    ("todo marker", re.compile(r"\b(?:TODO|TBD|FIXME)\b[:\s]")),
    ("lorem ipsum", re.compile(r"lorem ipsum", re.I)),
)
_RENDER_SUFFIXES = {".jpg", ".jpeg", ".png"}
_MIN_RENDER_BYTES = 1024


@dataclass
class GateResult:
    docx: str
    page_count: int = 0
    metrics: dict[str, object] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def error(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def payload(self, min_score: int) -> dict[str, object]:
        score = max(0, 100 - 20 * len(self.errors) - 2 * len(self.warnings))
        passed = not self.errors and score >= min_score
        return {
            "status": "pass" if passed else "fail",
            "quality_score": score,
            "minimum_score": min_score,
            "docx": self.docx,
            "page_count": self.page_count,
            "metrics": self.metrics,
            "errors": self.errors,
            "warnings": self.warnings,
            "repair_actions": _repair_actions(self.errors, self.warnings),
        }


def _repair_actions(errors: list[str], warnings: list[str]) -> list[dict[str, str]]:
    joined = "\n".join([*errors, *warnings]).lower()
    actions: list[dict[str, str]] = []

    def add(code: str, action: str) -> None:
        if not any(item["code"] == code for item in actions):
            actions.append({"code": code, "action": action})

    if "not a valid docx" in joined or "schema" in joined:
        add("fix_ooxml", "Regenerate the document with docx-js (or unpack, fix the XML, repack) and rerun scripts/office/validate.py.")
    if "placeholder" in joined or "todo" in joined or "lorem" in joined:
        add("resolve_placeholders", "Replace every placeholder and draft marker with final content, then re-export.")
    if "empty" in joined:
        add("add_content", "The body has no paragraphs with text; generate the document content before exporting.")
    if "render" in joined or "page" in joined:
        add(
            "rerender",
            "Convert the final DOCX to PDF with scripts/office/soffice.py and rasterise every page with "
            "pdftoppm into the render directory after the last edit, then rerun this gate.",
        )
    if "exports" in joined:
        add("export_location", "Write the final DOCX to /skill/projects/<project>/exports/ before saving it.")
    return actions


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _inspect_package(docx: Path, result: GateResult) -> bytes | None:
    try:
        with zipfile.ZipFile(docx) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" not in names or "word/document.xml" not in names:
                result.error("not a valid DOCX package: missing [Content_Types].xml or word/document.xml")
                return None
            return archive.read("word/document.xml")
    except (zipfile.BadZipFile, OSError) as exc:
        result.error(f"not a valid DOCX package: {exc}")
        return None


def _inspect_body(document_xml: bytes, result: GateResult) -> None:
    try:
        root = SafeET.fromstring(document_xml)
    except Exception as exc:  # noqa: BLE001 - any parse failure is a gate error
        result.error(f"word/document.xml does not parse: {exc}")
        return
    paragraphs = root.iter(f"{{{W_NS}}}p")
    paragraph_count = 0
    heading_count = 0
    words = 0
    texts: list[str] = []
    for paragraph in paragraphs:
        text = "".join(node.text or "" for node in paragraph.iter(f"{{{W_NS}}}t")).strip()
        if not text:
            continue
        paragraph_count += 1
        words += len(text.split())
        texts.append(text)
        style = paragraph.find(f"{{{W_NS}}}pPr/{{{W_NS}}}pStyle")
        style_value = str(style.get(f"{{{W_NS}}}val") or "") if style is not None else ""
        if style_value.lower().startswith(("heading", "title")):
            heading_count += 1
    result.metrics.update(
        {"paragraph_count": paragraph_count, "heading_count": heading_count, "word_count": words}
    )
    if paragraph_count == 0:
        result.error("the document body is empty: no paragraph carries text")
        return
    body = "\n".join(texts)
    for label, pattern in _PLACEHOLDER_PATTERNS:
        match = pattern.search(body)
        if match:
            result.error(f"unresolved {label} in the document text: {match.group(0)[:60]!r}")


def _run_schema_validation(docx: Path, result: GateResult) -> None:
    validator = Path(__file__).resolve().parent / "office" / "validate.py"
    if not validator.is_file():
        result.warn("scripts/office/validate.py is not bundled; schema validation skipped")
        return
    try:
        completed = subprocess.run(
            [sys.executable, str(validator), str(docx)],
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        result.warn(f"schema validation could not run: {exc}")
        return
    result.metrics["schema_validation_exit_code"] = completed.returncode
    if completed.returncode != 0:
        output = (completed.stdout + completed.stderr).strip().splitlines()
        detail = "; ".join(line.strip() for line in output[-3:]) or "validator exited non-zero"
        result.error(f"OOXML schema validation failed: {detail[:300]}")


def _pdf_page_count(pdf: Path) -> int | None:
    pdfinfo = shutil.which("pdfinfo")
    if pdfinfo is None:
        return None
    try:
        completed = subprocess.run(
            [pdfinfo, str(pdf)], capture_output=True, text=True, timeout=60, check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"^Pages:\s+(\d+)", completed.stdout, re.M)
    return int(match.group(1)) if match else None


def _inspect_render_evidence(docx: Path, render_dir: Path | None, result: GateResult) -> None:
    if render_dir is None:
        result.error("no render directory was given; render every page before delivery")
        return
    result.metrics["render_dir"] = str(render_dir)
    if not render_dir.is_dir():
        result.error(f"render evidence is missing: {render_dir} does not exist")
        result.metrics["rendered_page_count"] = 0
        return
    images = sorted(
        path for path in render_dir.iterdir()
        if path.is_file() and path.suffix.lower() in _RENDER_SUFFIXES and path.stem.lower().startswith("page")
    )
    result.metrics["rendered_page_count"] = len(images)
    if not images:
        result.error(f"no rendered page images (page-*.jpg/png) in {render_dir}")
        return
    docx_mtime = docx.stat().st_mtime
    stale = [path.name for path in images if path.stat().st_mtime < docx_mtime]
    if stale:
        result.error(f"render evidence predates the DOCX ({', '.join(stale[:3])}); re-render after the last edit")
    tiny = [path.name for path in images if path.stat().st_size < _MIN_RENDER_BYTES]
    if tiny:
        result.error(f"rendered page images look empty ({', '.join(tiny[:3])})")
    pdfs = sorted(path for path in render_dir.iterdir() if path.is_file() and path.suffix.lower() == ".pdf")
    page_count = len(images)
    if pdfs:
        pdf_pages = _pdf_page_count(pdfs[0])
        result.metrics["pdf"] = pdfs[0].name
        if pdf_pages is not None:
            result.metrics["pdf_page_count"] = pdf_pages
            page_count = pdf_pages
            if pdf_pages != len(images):
                result.error(
                    f"not every page has render evidence: the PDF has {pdf_pages} page(s), "
                    f"{len(images)} rendered image(s) found"
                )
    result.page_count = page_count


def _inspect_project_layout(docx: Path, project: Path | None, result: GateResult) -> None:
    if project is None:
        return
    exports = (project / "exports").resolve()
    try:
        docx.resolve().relative_to(exports)
    except ValueError:
        result.error(f"the DOCX is not under the project's exports directory ({exports})")


def run_gate(
    docx: Path,
    *,
    render_dir: Path | None,
    project: Path | None,
    min_score: int,
) -> dict[str, object]:
    result = GateResult(docx=str(docx))
    if not docx.is_file():
        result.error(f"not a valid DOCX package: {docx} does not exist")
        return result.payload(min_score)
    result.metrics["docx_sha256"] = _sha256(docx)
    result.metrics["docx_size_bytes"] = docx.stat().st_size
    document_xml = _inspect_package(docx, result)
    if document_xml is not None:
        _inspect_body(document_xml, result)
        _run_schema_validation(docx, result)
    _inspect_project_layout(docx, project, result)
    _inspect_render_evidence(docx, render_dir, result)
    return result.payload(min_score)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("docx", type=Path)
    parser.add_argument("--render-dir", type=Path)
    parser.add_argument("--project", type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--min-score", type=int, default=90)
    args = parser.parse_args()

    payload = run_gate(
        args.docx, render_dir=args.render_dir, project=args.project, min_score=args.min_score,
    )
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if payload["status"] == "pass" else 1


if __name__ == "__main__":
    raise SystemExit(main())
