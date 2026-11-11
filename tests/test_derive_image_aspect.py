"""Deriving another aspect ratio from one image, by centred crop or whole on a backdrop, for any media scenario."""

import pytest

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.tool_effect_classification import RuntimeToolEffect
from packages.core.ai.tools import media_tools


@pytest.mark.parametrize("width,height,aspect,expected", [
    # A portrait music-video frame becomes a 16:9 thumbnail: keep the full width, crop the height.
    (720, 1280, (16, 9), (720, 405)),
    # A landscape product photo becomes a 4:5 listing image: keep the full height, crop the width.
    (1600, 1000, (4, 5), (800, 1000)),
    # Already the requested ratio: nothing is cropped.
    (1920, 1080, (16, 9), (1920, 1080)),
])
def test_center_crop_keeps_the_largest_region_with_the_requested_ratio(width, height, aspect, expected):
    assert media_tools._center_crop_size(width, height, *aspect) == expected


def test_derive_image_aspect_is_registered_as_a_tool_with_a_required_source_and_ratio():
    names = {schema["function"]["name"]: schema for schema, _handler in media_tools.get_tools()}
    schema = names["derive_image_aspect"]["function"]
    assert schema["parameters"]["required"] == ["input_path", "aspect_ratio"]
    assert "16:9" in schema["parameters"]["properties"]["aspect_ratio"]["enum"]


def test_derive_image_aspect_has_a_declared_file_create_classification():
    # An undeclared tool is refused at runtime with blocked_by_authorization_configuration.
    classification = classify_runtime_tool("derive_image_aspect", {"input_path": "cover.png", "aspect_ratio": "16:9"})
    assert classification.effect is not RuntimeToolEffect.UNKNOWN
    assert classification.authorization is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("fit,cropped_fraction,edge_kept", [
    # Album art whose title runs to both side edges, derived for a 9:16 cover.
    ("crop", 0.4375, False),
    ("contain", 0.0, True),
])
async def test_contain_keeps_a_square_cover_whole_where_crop_cuts_its_edges(
    monkeypatch, tmp_path, fit, cropped_fraction, edge_kept,
):
    import json

    from PIL import Image, ImageDraw

    cover = Image.new("RGB", (400, 400), "black")
    draw = ImageDraw.Draw(cover)
    draw.rectangle((0, 0, 19, 399), fill="white")
    draw.rectangle((380, 0, 399, 399), fill="white")
    cover.save(tmp_path / "cover.png")
    registered = []

    async def base_dir(**_kwargs):
        return ""

    async def register(**kwargs):
        registered.append(kwargs)
        return "doc-derived"

    async def bind(**_kwargs):
        return None

    monkeypatch.setattr("packages.core.services.entity_fs.get_entity_root", lambda _entity_id: str(tmp_path))
    monkeypatch.setattr(media_tools, "_workspace_media_base_dir", base_dir)
    monkeypatch.setattr(media_tools, "_register_file_artifact", register)
    monkeypatch.setattr(media_tools, "_bind_artifact_to_workspace", bind)

    result = json.loads(await media_tools._derive_image_aspect_handler(
        entity_id="entity-1", input_path="cover.png", aspect_ratio="9:16", fit=fit, output_name="cover-9x16.png",
    ))

    assert result["ready"] is True, result
    assert result["cropped_fraction"] == cropped_fraction
    assert registered[0]["generation"]["cropped_fraction"] == cropped_fraction
    with Image.open(tmp_path / result["poster"]["fs_path"]) as derived:
        # Both fits give the frame a crop would have; only contain keeps the edges of the picture.
        assert derived.size == (225, 400)
        left_edge = derived.convert("L").getpixel((3, 200))
        right_edge = derived.convert("L").getpixel((221, 200))
    assert (left_edge > 200 and right_edge > 200) is edge_kept
