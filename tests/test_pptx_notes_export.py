"""Speaker notes must not leave dangling OOXML relationships."""

import sys
from pathlib import Path
from zipfile import ZipFile


SCRIPTS = Path(__file__).resolve().parents[1] / "packages/core/ai/skills/pptx/scripts"
sys.path.insert(0, str(SCRIPTS))

from svg_to_pptx import create_pptx_with_native_svg  # noqa: E402
from pptx_quality_gate import validate_relationships  # noqa: E402


def test_exported_notes_have_a_notes_master(tmp_path: Path) -> None:
    svg = tmp_path / "01_test.svg"
    svg.write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" width="1280" height="720" '
        'viewBox="0 0 1280 720"><rect width="1280" height="720" '
        'fill="#ffffff"/></svg>',
        encoding="utf-8",
    )
    output = tmp_path / "notes.pptx"

    assert create_pptx_with_native_svg(
        [svg], output, notes={svg.stem: "Speaker context."},
        use_compat_mode=False, use_native_shapes=True, verbose=False,
    )

    with ZipFile(output) as package:
        assert "ppt/notesMasters/notesMaster1.xml" in package.namelist()
    assert validate_relationships(output) == []
