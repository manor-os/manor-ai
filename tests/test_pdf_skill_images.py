from __future__ import annotations

import json
from pathlib import Path

import pytest
from PIL import Image
from pypdf import PdfReader

pytest.importorskip("fpdf")

from fpdf import FPDF  # noqa: E402
from packages.core.ai.skills.pdf.scripts.prepare_image_asset import (  # noqa: E402
    prepare_image_asset,
)


def test_prepare_generated_image_resizes_preserves_alpha_and_records_provenance(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source.png"
    output = tmp_path / "assets" / "hero.png"
    manifest = tmp_path / "assets" / "manifest.json"
    Image.new("RGBA", (400, 200), (20, 40, 80, 160)).save(source)

    record = prepare_image_asset(
        source,
        output,
        source_type="generated",
        source_ref="workspace:generated/hero.png",
        prompt="Abstract competitive arena, navy and orange, no text",
        manifest_path=manifest,
        max_long_edge=200,
    )

    with Image.open(output) as prepared:
        assert prepared.size == (200, 100)
        assert prepared.mode == "RGBA"
    assert record["source_type"] == "generated"
    assert record["generation_prompt"].endswith("no text")
    assert record["resized"] is True
    assert len(record["sha256"]) == 64
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    assert payload["version"] == 1
    assert payload["assets"] == [record]


def test_prepare_downloaded_image_requires_source_and_rights(tmp_path: Path) -> None:
    source = tmp_path / "download.png"
    Image.new("RGB", (80, 60), "blue").save(source)

    with pytest.raises(ValueError, match="source_ref provenance"):
        prepare_image_asset(source, tmp_path / "out.png", source_type="downloaded")

    with pytest.raises(ValueError, match="explicit license"):
        prepare_image_asset(
            source,
            tmp_path / "out.png",
            source_type="downloaded",
            source_ref="https://example.test/source-page",
        )


def test_prepared_image_embeds_in_a_valid_pdf(tmp_path: Path) -> None:
    source = tmp_path / "source.png"
    prepared = tmp_path / "prepared.jpg"
    pdf_path = tmp_path / "with-image.pdf"
    Image.new("RGB", (600, 300), (28, 66, 112)).save(source)
    prepare_image_asset(source, prepared, source_type="user")

    pdf = FPDF()
    pdf.add_page()
    pdf.image(str(prepared), x=16, y=24, w=178)
    pdf.output(str(pdf_path))

    reader = PdfReader(pdf_path)
    assert len(reader.pages) == 1
    resources = reader.pages[0]["/Resources"]
    xobjects = resources["/XObject"].get_object()
    assert any(obj.get_object().get("/Subtype") == "/Image" for obj in xobjects.values())
