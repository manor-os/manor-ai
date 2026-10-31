"""Centred crops that derive another aspect ratio from one image, for any media scenario."""

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
