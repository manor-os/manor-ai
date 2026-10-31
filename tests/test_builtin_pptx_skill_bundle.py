"""The bundled PPTX instructions must not point at files absent from the sandbox."""

from pathlib import Path

import pytest


PPTX_SKILL = Path("packages/core/ai/skills/pptx")


@pytest.mark.parametrize(
    "relative_path",
    [
        "references/layout-contract.md",
        "references/text-role-system.md",
        "references/typography-profiles.md",
        "scripts/icon_composer.py",
        "scripts/render_page_images.py",
        "scripts/render_pptx.py",
        "templates/layouts/composition_registry.json",
        "templates/layouts/typography_profiles.json",
        "workflows/embedded-media.md",
        "workflows/follow-reference-pptx.md",
        "workflows/icon-composer.md",
        "workflows/native-charts.md",
        "workflows/free-design.md",
        "scripts/build_deck.py",
        "templates/decks/styles.json",
    ],
)
def test_pptx_skill_documented_asset_is_bundled(relative_path: str) -> None:
    # Free-design assets are documented in the free-design workflow, which
    # SKILL.md routes to; template-mode assets are documented in SKILL.md.
    instructions = "\n".join(
        (PPTX_SKILL / name).read_text(encoding="utf-8")
        for name in ("SKILL.md", "workflows/free-design.md")
    )
    assert relative_path in instructions
    asset = PPTX_SKILL / relative_path
    assert asset.is_file(), f"PPTX skill references a missing sandbox asset: {relative_path}"
    assert asset.read_text(encoding="utf-8").strip()
