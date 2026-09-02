from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILLS_ROOT = ROOT / "packages" / "core" / "ai" / "skills"


def _requirement_names(skill_name: str) -> set[str]:
    requirements = SKILLS_ROOT / skill_name / "requirements.txt"
    names: set[str] = set()
    for raw_line in requirements.read_text().splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        match = re.match(r"[A-Za-z0-9_.-]+", line)
        assert match is not None, f"invalid requirement in {requirements}: {raw_line}"
        names.add(match.group(0).lower().replace("_", "-"))
    return names


def test_every_builtin_sandbox_skill_declares_its_runtime_dependencies() -> None:
    expected_python = {
        "docx": {"defusedxml", "lxml"},
        "pdf": {
            "fpdf2",
            "pandas",
            "pdf2image",
            "pdfplumber",
            "pillow",
            "pypdf",
            "pytesseract",
            "reportlab",
        },
        "pptx": {"defusedxml", "pillow", "python-pptx"},
        "xlsx": {"defusedxml", "lxml", "openpyxl", "pandas"},
    }

    sandbox_skills = {
        config.parent.name
        for config in SKILLS_ROOT.glob("*/config.json")
        if json.loads(config.read_text()).get("type") == "sandbox"
    }
    assert sandbox_skills == set(expected_python)

    for skill_name, expected in expected_python.items():
        assert expected <= _requirement_names(skill_name)


def test_docx_declares_its_javascript_runtime_dependency() -> None:
    package = json.loads((SKILLS_ROOT / "docx" / "package.json").read_text())

    assert package["private"] is True
    assert package["dependencies"] == {"docx": "9.7.1"}
