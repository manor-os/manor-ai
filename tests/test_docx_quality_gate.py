"""The docx skill's final quality gate: the skill-side half of the server
gate in sandbox_tools (which had no script to run since it was added)."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "packages/core/ai/skills/docx/scripts"
GATE = SCRIPTS / "docx_quality_gate.py"
sys.path.insert(0, str(SCRIPTS))

import docx_quality_gate  # noqa: E402

docx = pytest.importorskip("docx")
PIL = pytest.importorskip("PIL")
from PIL import Image  # noqa: E402


def _document(path: Path, *, paragraphs: list[str], heading: str | None = "Wave B") -> None:
    document = docx.Document()
    if heading:
        document.add_heading(heading, level=1)
    for text in paragraphs:
        document.add_paragraph(text)
    path.parent.mkdir(parents=True, exist_ok=True)
    document.save(path)
    _repair_python_docx_settings(path)


def _repair_python_docx_settings(path: Path) -> None:
    """python-docx's default template ships ``<w:zoom/>`` without the
    schema-required ``w:percent``; the skill's validator (rightly) rejects
    it. A docx-js document does not have this defect."""
    import re
    import shutil
    import zipfile

    fixed = path.with_suffix(".fixed.docx")
    with zipfile.ZipFile(path) as source, zipfile.ZipFile(fixed, "w", zipfile.ZIP_DEFLATED) as target:
        for item in source.infolist():
            data = source.read(item.filename)
            if item.filename == "word/settings.xml":
                text = data.decode("utf-8")
                if "w:percent" not in text:
                    text = re.sub(r"<w:zoom(\s[^>]*)?/>", lambda m: f'<w:zoom{m.group(1) or ""} w:percent="100"/>', text, count=1)
                data = text.encode("utf-8")
            target.writestr(item, data)
    shutil.move(fixed, path)


def _render(render_dir: Path, pages: int, *, after: Path | None = None) -> None:
    render_dir.mkdir(parents=True, exist_ok=True)
    if after is not None:
        # Evidence must post-date the DOCX; file mtimes have 1 s granularity on some FS.
        later = after.stat().st_mtime + 2
    for index in range(1, pages + 1):
        target = render_dir / f"page-{index}.jpg"
        Image.new("RGB", (400, 520), (250, 250, 250)).save(target, "JPEG", quality=90)
        if after is not None:
            os.utime(target, (later, later))


def _project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "projects" / "wave-b"
    return project, project / "exports" / "wave-b.docx", project / "qa" / "final-render"


def test_clean_document_with_render_evidence_passes(tmp_path: Path) -> None:
    project, final, render_dir = _project(tmp_path)
    _document(final, paragraphs=["The water supply will be shut off on Tuesday between 9 and 11."])
    _render(render_dir, 1, after=final)

    payload = docx_quality_gate.run_gate(final, render_dir=render_dir, project=project, min_score=90)

    assert payload["errors"] == []
    assert payload["status"] == "pass"
    assert payload["quality_score"] >= 90
    assert payload["page_count"] == 1
    assert payload["metrics"]["rendered_page_count"] == 1
    assert payload["metrics"]["docx_sha256"] == hashlib.sha256(final.read_bytes()).hexdigest()
    assert payload["metrics"]["docx_size_bytes"] == final.stat().st_size
    assert payload["metrics"]["paragraph_count"] == 2
    assert payload["metrics"]["heading_count"] == 1
    assert payload["metrics"].get("schema_validation_exit_code") == 0


def test_report_satisfies_the_server_side_evidence_check(tmp_path: Path) -> None:
    """The report this script writes is what sandbox_tools verifies."""
    import packages.core.ai.tools.sandbox_tools as sandbox_tools

    project, final, render_dir = _project(tmp_path)
    _document(final, paragraphs=["Final content."])
    _render(render_dir, 1, after=final)
    report = project / "qa" / "docx-quality.json"

    completed = subprocess.run(
        [
            sys.executable, str(GATE), str(final), "--project", str(project),
            "--render-dir", str(render_dir), "--min-score", "90", "--report", str(report),
        ],
        capture_output=True, text=True, check=False,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    error, payload = sandbox_tools._docx_quality_evidence_error(
        file_path=str(final),
        content_bytes=final.read_bytes(),
        gate_exit_code=completed.returncode,
        report_bytes=report.read_bytes(),
    )
    assert error is None, error
    assert payload["status"] == "pass"


def test_placeholders_and_empty_bodies_fail(tmp_path: Path) -> None:
    project, final, render_dir = _project(tmp_path)
    _document(final, paragraphs=["Dear {{customer_name}}, see [INSERT DATE]."])
    _render(render_dir, 1, after=final)
    payload = docx_quality_gate.run_gate(final, render_dir=render_dir, project=project, min_score=90)
    assert payload["status"] == "fail"
    assert any("placeholder" in item for item in payload["errors"])
    assert any(action["code"] == "resolve_placeholders" for action in payload["repair_actions"])

    empty = project / "exports" / "empty.docx"
    _document(empty, paragraphs=[], heading=None)
    _render(render_dir, 1, after=empty)
    payload = docx_quality_gate.run_gate(empty, render_dir=render_dir, project=project, min_score=90)
    assert any("empty" in item for item in payload["errors"])


def test_render_evidence_must_exist_be_complete_and_fresh(tmp_path: Path) -> None:
    project, final, render_dir = _project(tmp_path)
    _document(final, paragraphs=["Content."])

    missing = docx_quality_gate.run_gate(final, render_dir=render_dir, project=project, min_score=90)
    assert any("render evidence is missing" in item for item in missing["errors"])
    assert missing["metrics"]["rendered_page_count"] == 0

    _render(render_dir, 1)
    stale_time = final.stat().st_mtime - 60
    os.utime(render_dir / "page-1.jpg", (stale_time, stale_time))
    stale = docx_quality_gate.run_gate(final, render_dir=render_dir, project=project, min_score=90)
    assert any("predates the DOCX" in item for item in stale["errors"])

    if docx_quality_gate.shutil.which("pdfinfo"):
        # A PDF with more pages than rendered images means a page was skipped.
        _render(render_dir, 1, after=final)
        pdf = render_dir / "wave-b.pdf"
        pdf.write_bytes(_two_page_pdf())
        short = docx_quality_gate.run_gate(final, render_dir=render_dir, project=project, min_score=90)
        assert short["metrics"].get("pdf_page_count") == 2
        assert any("not every page has render evidence" in item for item in short["errors"])


def test_final_file_must_live_under_the_project_exports(tmp_path: Path) -> None:
    project = tmp_path / "projects" / "wave-b"
    stray = tmp_path / "wave-b.docx"
    _document(stray, paragraphs=["Content."])
    render_dir = project / "qa" / "final-render"
    _render(render_dir, 1, after=stray)
    payload = docx_quality_gate.run_gate(stray, render_dir=render_dir, project=project, min_score=90)
    assert any("exports directory" in item for item in payload["errors"])
    assert any(action["code"] == "export_location" for action in payload["repair_actions"])


def test_a_non_docx_file_is_rejected(tmp_path: Path) -> None:
    bogus = tmp_path / "projects" / "p" / "exports" / "bogus.docx"
    bogus.parent.mkdir(parents=True)
    bogus.write_bytes(b"not a zip")
    payload = docx_quality_gate.run_gate(bogus, render_dir=None, project=bogus.parents[1], min_score=90)
    assert payload["status"] == "fail"
    assert any("not a valid DOCX package" in item for item in payload["errors"])


def _two_page_pdf() -> bytes:
    """Smallest well-formed two-page PDF (enough for pdfinfo)."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R 4 0 R] /Count 2 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 200] >>",
    ]
    out = "%PDF-1.4\n"
    offsets = []
    for index, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{index} 0 obj\n{body}\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n"
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n"
    return out.encode("latin-1")
