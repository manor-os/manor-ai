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
            "pymupdf",
            "pytesseract",
            "reportlab",
        },
        "pptx": {"defusedxml", "pillow", "python-pptx"},
        "remotion-best-practices": set(),
        "remotion-captions": set(),
        "remotion-create": set(),
        "remotion-docs": set(),
        "remotion-interactivity": set(),
        "remotion-maps": set(),
        "remotion-markup": set(),
        "remotion-multimedia": set(),
        "remotion-render": set(),
        "remotion-saas": set(),
        "remotion-studio": set(),
        "remotion-upgrade": set(),
        "xlsx": {"defusedxml", "lxml", "openpyxl", "pandas"},
    }

    sandbox_skills = {
        config.parent.name
        for config in SKILLS_ROOT.glob("*/config.json")
        if json.loads(config.read_text()).get("type") == "sandbox"
    }
    assert sandbox_skills == set(expected_python)

    for skill_name, expected in expected_python.items():
        if expected:
            assert expected <= _requirement_names(skill_name)


def test_docx_declares_its_javascript_runtime_dependency() -> None:
    package = json.loads((SKILLS_ROOT / "docx" / "package.json").read_text())

    assert package["private"] is True
    assert package["dependencies"] == {"docx": "9.7.1"}


def test_remotion_skills_declare_a_pinned_javascript_runtime() -> None:
    for skill_name in (
        "remotion-best-practices",
        "remotion-captions",
        "remotion-create",
        "remotion-docs",
        "remotion-interactivity",
        "remotion-maps",
        "remotion-markup",
        "remotion-multimedia",
        "remotion-render",
        "remotion-saas",
        "remotion-studio",
        "remotion-upgrade",
    ):
        package = json.loads(
            (SKILLS_ROOT / skill_name / "package.json").read_text()
        )

        assert package["private"] is True
        assert package["dependencies"]["@remotion/cli"] == "4.0.506"
        assert package["dependencies"]["remotion"] == "4.0.506"
        assert package["dependencies"]["react"] == "19.2.3"
        assert all(
            not version.startswith(("^", "~"))
            for version in package["dependencies"].values()
        )
