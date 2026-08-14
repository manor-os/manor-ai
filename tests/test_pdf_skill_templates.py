from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from pypdf import PdfReader

pytest.importorskip("fpdf")

from packages.core.ai.skills.pdf.scripts.generate_from_template import (  # noqa: E402
    SKILL_ROOT,
    TEMPLATES_DIR,
    generate_from_template,
    list_templates,
)


def test_pdf_skill_requires_one_verified_canonical_delivery() -> None:
    instructions = (SKILL_ROOT / "SKILL.md").read_text(encoding="utf-8")

    assert "/skill/output/pdf/<descriptive-name>.pdf" in instructions
    assert "Save the final PDF exactly once" in instructions
    assert "Never save helper scripts" in instructions
    assert "Do not generate a DOCX substitute" in instructions
    assert "collection_font_number=2" in instructions
    assert 'generate_file(kind="image")' in instructions
    assert "prepare_image_asset.py" in instructions


@pytest.mark.parametrize("template_id", ["business-report", "project-proposal", "invoice", "academic-paper"])
def test_pdf_templates_generate_valid_pdfs_without_mutating_data(tmp_path: Path, template_id: str) -> None:
    source_data = TEMPLATES_DIR / "examples" / f"{template_id}.json"
    source_bytes = source_data.read_bytes()
    output = tmp_path / template_id / f"{template_id}.pdf"

    result = generate_from_template(template_id, source_data, output)

    assert result["template"] == template_id
    assert result["validated"] is True
    assert result["page_count"] >= 1
    assert output.stat().st_size > 1_500
    assert output.stat().st_mode & 0o777 == 0o644
    reader = PdfReader(output)
    assert len(reader.pages) == result["page_count"]
    searchable = "\n".join(page.extract_text() or "" for page in reader.pages)
    metadata = " ".join(str(value) for value in (reader.metadata or {}).values())
    assert "manor" not in f"{searchable} {metadata}".lower()
    assert source_data.read_bytes() == source_bytes


def test_pdf_template_manifest_matches_installed_specs() -> None:
    templates = list_templates()

    assert [template["id"] for template in templates] == [
        "business-report",
        "project-proposal",
        "invoice",
        "academic-paper",
    ]
    assert all((TEMPLATES_DIR / f"{template['id']}.json").is_file() for template in templates)
    assert all((TEMPLATES_DIR / "examples" / f"{template['id']}.json").is_file() for template in templates)


def test_showcase_assets_are_english_only_and_visually_distinct() -> None:
    cjk = re.compile(r"[\u2e80-\u9fff\uf900-\ufaff\u3040-\u30ff\uac00-\ud7af]")
    payloads = {path: path.read_text(encoding="utf-8") for path in sorted(TEMPLATES_DIR.rglob("*.json"))}

    assert all("manor" not in payload.lower() for payload in payloads.values())
    assert all(cjk.search(payload) is None for payload in payloads.values())

    specs = {
        template_id: json.loads((TEMPLATES_DIR / f"{template_id}.json").read_text(encoding="utf-8"))
        for template_id in ("business-report", "project-proposal", "invoice", "academic-paper")
    }
    assert specs["business-report"]["orientation"] == "L"
    assert specs["project-proposal"]["orientation"] == "P"
    assert specs["invoice"]["orientation"] == "P"
    assert specs["academic-paper"]["orientation"] == "P"
    assert len({spec["palette"]["primary"] for spec in specs.values()}) == 4


def test_academic_template_has_a_complete_three_page_paper_structure(tmp_path: Path) -> None:
    data_path = TEMPLATES_DIR / "examples" / "academic-paper.json"
    output = tmp_path / "academic-paper.pdf"

    result = generate_from_template("academic-paper", data_path, output)
    reader = PdfReader(output)
    searchable = "\n".join(page.extract_text() or "" for page in reader.pages)

    assert result["page_count"] == 3
    assert all(float(page.mediabox.height) > float(page.mediabox.width) for page in reader.pages)
    assert "ABSTRACT" in searchable
    assert "REFERENCES" in searchable


def test_pdf_template_rejects_missing_required_data(tmp_path: Path) -> None:
    data_path = tmp_path / "incomplete.json"
    data_path.write_text(json.dumps({"document_title": "Incomplete"}), encoding="utf-8")

    with pytest.raises(ValueError, match="Missing required template data"):
        generate_from_template("business-report", data_path, tmp_path / "output.pdf")
