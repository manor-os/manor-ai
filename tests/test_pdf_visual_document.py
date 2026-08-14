from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

pytest.importorskip("fpdf")

from packages.core.ai.skills.pdf.scripts.compose_visual_document import (  # noqa: E402
    generate_visual_document,
    validate_recipe,
)


def _make_image(path: Path, size: tuple[int, int], color: tuple[int, int, int]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path, quality=92)


def test_visual_document_composes_tables_callouts_images_galleries_and_contacts(tmp_path: Path) -> None:
    assets = tmp_path / "assets"
    _make_image(assets / "hero.jpg", (1500, 900), (36, 87, 131))
    _make_image(assets / "room-1.jpg", (900, 1200), (221, 205, 173))
    _make_image(assets / "room-2.jpg", (1400, 800), (203, 220, 227))
    _make_image(assets / "room-3.jpg", (1000, 1000), (149, 177, 160))
    _make_image(assets / "room-4.jpg", (1600, 900), (222, 190, 155))

    recipe = {
        "title": "Partner Housing Listings",
        "subtitle": "Image-led inventory and follow-up brief",
        "brand": "Fieldnote Housing",
        "page_size": "LETTER",
        "meta": ["Updated: 2026-08-10", "Audience: partner operations"],
        "footer": "Fieldnote Housing · Internal",
        "sections": [
            {
                "title": "Property One",
                "subtitle": "South Berkeley · walkable to campus",
                "blocks": [
                    {
                        "type": "key_value_table",
                        "rows": [
                            {"label": "Layout", "value": "2 bed / 2 bath"},
                            {"label": "Rent", "value": "$3,995 / month"},
                            {
                                "label": "Listing",
                                "value": "Open listing",
                                "link": "https://example.test/listing",
                            },
                        ],
                    },
                    {
                        "type": "callout",
                        "tone": "success",
                        "text": "Best current match; partnership intent confirmed.",
                    },
                    {
                        "type": "image",
                        "path": "assets/hero.jpg",
                        "caption": "Property exterior",
                        "height": 78,
                    },
                    {
                        "type": "gallery",
                        "columns": 2,
                        "items": [
                            {"path": "assets/room-1.jpg", "caption": "Living room"},
                            {"path": "assets/room-2.jpg", "caption": "Kitchen"},
                            {"path": "assets/room-3.jpg", "caption": "Bedroom one"},
                            {"path": "assets/room-4.jpg", "caption": "Bedroom two"},
                        ],
                    },
                ],
            },
            {
                "title": "Partners in follow-up",
                "blocks": [
                    {
                        "type": "contact_list",
                        "items": [
                            {
                                "name": "Oxford Property Management",
                                "details": "Ada Ormsby · ada@example.test · 510-555-0123",
                                "note": "No current availability; follow up in December.",
                            },
                            {
                                "name": "Telegraph Commons",
                                "details": "leasing@example.test",
                                "note": "Awaiting confirmation of layout and referral policy.",
                            },
                        ],
                    }
                ],
            },
        ],
    }
    recipe_path = tmp_path / "visual-document.json"
    recipe_path.write_text(json.dumps(recipe), encoding="utf-8")
    source_bytes = recipe_path.read_bytes()
    output = tmp_path / "output" / "partner-housing.pdf"

    result = generate_visual_document(recipe_path, output)

    assert result["validated"] is True
    assert result["image_count"] == 5
    assert result["block_count"] == 5
    assert result["page_count"] >= 2
    assert output.stat().st_size > 10_000
    assert output.stat().st_mode & 0o777 == 0o644
    assert recipe_path.read_bytes() == source_bytes

    reader = PdfReader(output)
    page_texts = [page.extract_text() or "" for page in reader.pages]
    searchable = "\n".join(page_texts)
    assert "Partner Housing Listings" in searchable
    assert "Property One" in searchable
    assert "Oxford Property Management" in searchable
    follow_up_page = next(text for text in page_texts if "Partners in follow-up" in text)
    assert "Oxford Property Management" in follow_up_page
    image_objects = 0
    uri_actions = 0
    for page in reader.pages:
        resources = page.get("/Resources") or {}
        xobjects = resources.get("/XObject")
        if xobjects:
            image_objects += sum(
                obj.get_object().get("/Subtype") == "/Image"
                for obj in xobjects.get_object().values()
            )
        for annotation in page.get("/Annots") or []:
            action = annotation.get_object().get("/A") or {}
            uri_actions += int(bool(action.get("/URI")))
    assert image_objects >= 5
    assert uri_actions >= 1


def test_visual_document_rejects_unknown_blocks_and_remote_images(tmp_path: Path) -> None:
    base = {
        "title": "Visual brief",
        "sections": [{"title": "Section", "blocks": [{"type": "paragraph", "text": "Copy"}]}],
    }
    invalid_block = json.loads(json.dumps(base))
    invalid_block["sections"][0]["blocks"][0]["type"] = "absolute_canvas"
    with pytest.raises(ValueError, match="Unsupported visual document block"):
        validate_recipe(invalid_block)

    remote_image = json.loads(json.dumps(base))
    remote_image["sections"][0]["blocks"] = [
        {"type": "image", "path": "https://example.test/image.jpg"}
    ]
    recipe_path = tmp_path / "remote-image.json"
    recipe_path.write_text(json.dumps(remote_image), encoding="utf-8")
    with pytest.raises(ValueError, match="Remote image URLs are not supported"):
        generate_visual_document(recipe_path, tmp_path / "should-not-exist.pdf")


def test_pdf_skill_routes_image_led_documents_to_semantic_composer() -> None:
    skill = Path("packages/core/ai/skills/pdf/SKILL.md").read_text(encoding="utf-8")
    reference = Path(
        "packages/core/ai/skills/pdf/references/visual-document-schema.md"
    ).read_text(encoding="utf-8")

    assert "Image-First Visual Documents" in skill
    assert "compose_visual_document.py" in skill
    assert "not a locked design template" in skill
    assert "key_value_table" in reference
    assert "gallery" in reference
    assert "verify_pdf.py" in reference
