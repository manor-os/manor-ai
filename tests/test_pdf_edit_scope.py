import json
import subprocess
import sys
from pathlib import Path

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from packages.core.ai.skills.pdf.scripts.verify_pdf import verify_pdf


def _write_pdf(path: Path, texts: list[str]) -> None:
    writer = PdfWriter()
    font = writer._add_object(
        DictionaryObject(
            {
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }
        )
    )
    for text in texts:
        page = writer.add_blank_page(width=612, height=792)
        page[NameObject("/Resources")] = DictionaryObject(
            {
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            }
        )
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 700 Td ({text}) Tj ET".encode("ascii"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as handle:
        writer.write(handle)


@pytest.mark.parametrize(
    ("output_texts", "passed", "missing", "remaining"),
    [
        (["RetainedTrust 60%", "Retained Trust 30%"], True, [], []),
        (["60%", "Retained Trust Retained Trust 30%"], False, [1], []),
        (["Retained Trust 60%", "Retained Trust 30% - Former RemovedCorporation"], False, [], [2, 2]),
        (["Retained Trust 60%"], False, [2], []),
        (["Retained Trust ? 60%", "Retained Trust 30%"], False, [], []),
    ],
)
def test_edit_scope_checks_each_original_page_and_all_target_aliases(
    tmp_path: Path, output_texts: list[str], passed: bool, missing: list[int], remaining: list[int]
) -> None:
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _write_pdf(source, ["Retained Trust 60% Removed Corp", "Retained Trust 30% Removed Corporation"])
    _write_pdf(output, output_texts)
    original = source.read_bytes()
    result = verify_pdf(
        output,
        source_pdf=source,
        remove_text=["Removed Corp", "Removed Corporation"],
        preserve_text=["Retained Trust"],
        run_external_checks=False,
    )
    assert result["machine_checks_passed"] is passed
    assert result["edit_scope_passed"] is passed
    assert [item["page"] for item in result["missing_retained_labels"]] == missing
    assert [item["page"] for item in result["remaining_removed_labels"]] == remaining
    assert source.read_bytes() == original


@pytest.mark.parametrize("option", ["remove_text", "preserve_text"])
def test_edit_scope_rejects_labels_absent_from_the_source(tmp_path: Path, option: str) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source, ["Retained Trust"])
    result = verify_pdf(source, source_pdf=source, run_external_checks=False, **{option: ["Typo"]})
    assert result["machine_checks_passed"] is False
    assert "absent from the source" in result["errors"][0]


def test_edit_scope_requires_a_source_for_labels(tmp_path: Path) -> None:
    source = tmp_path / "source.pdf"
    _write_pdf(source, ["Retained Trust"])
    with pytest.raises(ValueError, match="require --source"):
        verify_pdf(source, preserve_text=["Retained Trust"], run_external_checks=False)


def test_edit_report_can_be_corrected_but_final_gate_still_rejects_missing_labels(tmp_path: Path) -> None:
    source, output = tmp_path / "source.pdf", tmp_path / "output.pdf"
    _write_pdf(source, ["Retained Trust Removed Corp"])
    _write_pdf(output, ["Retained Trust"])
    script = Path(__file__).resolve().parents[1] / "packages/core/ai/skills/pdf/scripts/verify_pdf.py"
    command = [
        sys.executable,
        str(script),
        str(output),
        str(tmp_path / "rendered"),
        "--source",
        str(source),
        "--remove-text",
        "Removed Corp",
        "--preserve-text",
        "Retained Trust",
    ]
    # A missing party is a correctable finding, while the final CLI remains a gate.
    _write_pdf(output, ["Edited paragraph"])
    report = subprocess.run([*command, "--report-only"], capture_output=True, text=True)
    assert report.returncode == 0, report.stderr
    assert json.loads(report.stdout)["machine_checks_passed"] is False
    assert json.loads(report.stdout)["missing_retained_labels"][0]["label"] == "Retained Trust"
    final = subprocess.run(command, capture_output=True, text=True)
    assert final.returncode == 1
    assert json.loads(final.stdout)["edit_scope_passed"] is False
    _write_pdf(output, ["Retained Trust"])
    corrected = subprocess.run(command, capture_output=True, text=True)
    assert corrected.returncode == 0, corrected.stderr
    assert json.loads(corrected.stdout)["machine_checks_passed"] is True
