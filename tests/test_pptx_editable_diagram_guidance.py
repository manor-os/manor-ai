from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_pptx_skill_links_editable_diagram_guidance() -> None:
    skill = (ROOT / "packages/core/ai/skills/pptx/SKILL.md").read_text(encoding="utf-8")

    assert "references/blocks/editable-diagram.md" in skill
    assert "native PPTX shapes/connectors/text" in skill


def test_editable_diagram_guidance_rejects_flattened_raster_output() -> None:
    guidance = (ROOT / "packages/core/ai/skills/pptx/references/blocks/editable-diagram.md").read_text(encoding="utf-8")

    assert "Do not flatten diagrams into a single screenshot-like image" in guidance
    assert "text boxes for all labels" in guidance
    assert "native connectors or editable lines" in guidance
    assert "Object count should show many shapes/connectors/text runs" in guidance


def test_existing_diagram_block_defers_to_editable_pptx_guidance() -> None:
    guidance = (ROOT / "packages/core/ai/skills/pptx/references/blocks/diagram.md").read_text(encoding="utf-8")

    assert "editable-diagram.md" in guidance
    assert "原生 PPTX shapes/connectors/text" in guidance
