import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "packages/core/ai/skills/pptx/scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

from svg_quality_checker import SVGQualityChecker  # noqa: E402


def test_svg_quality_checker_rejects_low_contrast_text_on_solid_panel(tmp_path: Path) -> None:
    svg = tmp_path / "low_contrast.svg"
    svg.write_text(
        """<svg viewBox="0 0 1600 900" xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="0" width="1600" height="900" fill="#070B18"/>
  <rect x="200" y="160" width="720" height="110" fill="#FFFFFF"/>
  <text x="240" y="230" fill="#F5F7FF" font-size="56">MANOR AI</text>
</svg>
""",
        encoding="utf-8",
    )

    result = SVGQualityChecker().check_file(str(svg))

    assert not result["passed"]
    assert any("Low-contrast text" in error and "MANOR AI" in error for error in result["errors"])


def test_svg_quality_checker_allows_readable_text_on_solid_panel(tmp_path: Path) -> None:
    svg = tmp_path / "readable.svg"
    svg.write_text(
        """<svg viewBox="0 0 1600 900" xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="0" width="1600" height="900" fill="#FFFFFF"/>
  <text x="200" y="220" fill="#111827" font-size="56">Readable Title</text>
</svg>
""",
        encoding="utf-8",
    )

    result = SVGQualityChecker().check_file(str(svg))

    assert result["passed"]
    assert not any("Low-contrast text" in warning for warning in result["warnings"])
