"""Native slide backgrounds share one generation/template/patch operation."""
from __future__ import annotations

import io

import pytest
from pptx import Presentation

from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
from packages.core.ai.tools.file_tools import _apply_office_patch_sequence_sync
from packages.core.contracts.file_engine import file_patch_operations, normalize_file_patch_operation
from packages.core.services.file_engine_patches import describe_file_structure


def _operations(*operations: dict) -> list[dict]:
    return [normalize_file_patch_operation(operation) for operation in operations]


def _seed() -> list[dict]:
    return _operations({"op": "slide.insert", "index": 0, "layout_index": 6})


def _gradient() -> dict:
    return {
        "op": "slide.format",
        "slide": 1,
        "format": {
            "background_gradient": {
                "angle": 35.5,
                "stops": [
                    {"position": 0, "color": "112244", "opacity": 1},
                    {"position": 0.55, "color": "336699", "opacity": 0.75},
                    {"position": 1, "color": "88CCEE", "opacity": 1},
                ],
            },
        },
    }


def _background_xml(data: bytes):
    presentation = Presentation(io.BytesIO(data))
    slide = presentation.slides[0]
    namespace = "{http://schemas.openxmlformats.org/presentationml/2006/main}"
    return slide, slide._element.cSld.find(namespace + "bg")


def _assert_gradient(data: bytes) -> None:
    slide, background = _background_xml(data)
    assert slide.follow_master_background is False
    assert background is not None
    drawing = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    gradient = background.find(
        "{http://schemas.openxmlformats.org/presentationml/2006/main}bgPr/" + drawing + "gradFill",
    )
    assert gradient is not None
    linear = gradient.find(drawing + "lin")
    assert linear is not None and int(linear.get("ang")) == 2_130_000
    stops = gradient.findall(drawing + "gsLst/" + drawing + "gs")
    assert [int(stop.get("pos")) for stop in stops] == [0, 55_000, 100_000]
    assert [stop.find(drawing + "srgbClr").get("val") for stop in stops] == [
        "112244", "336699", "88CCEE",
    ]
    assert [
        int(stop.find(drawing + "srgbClr/" + drawing + "alpha").get("val"))
        for stop in stops
    ] == [100_000, 75_000, 100_000]


def test_slide_format_is_shared_by_blank_generation_template_generation_and_patch(tmp_path):
    assert "slide.format" in file_patch_operations("pptx")
    operation = normalize_file_patch_operation(_gradient())

    blank = _generate_office_operations_sync("pptx", [*_seed(), operation])
    assert not blank.get("error"), blank

    template = _generate_office_operations_sync("pptx", _seed())["_persisted_bytes"]
    templated = _generate_office_operations_sync(
        "pptx", [operation], template_bytes=template,
    )
    path = tmp_path / "background-template.pptx"
    path.write_bytes(template)
    patched = _apply_office_patch_sequence_sync(str(path), [operation])
    for result in (blank, templated, patched):
        assert not result.get("error"), result
        assert result["operation_results"][-1] == {
            "operation": "slide.format",
            "slide": 1,
            "follow_master_background": False,
        }
        _assert_gradient(result["_persisted_bytes"])
    assert path.read_bytes() == template

    output = tmp_path / "gradient.pptx"
    output.write_bytes(patched["_persisted_bytes"])
    slide = describe_file_structure(str(output))["slides"][0]
    assert slide["background_type"] == "gradient"
    assert slide["follow_master_background"] is False
    assert slide["format"]["background_gradient"] == {
        "angle": 35.5,
        "stops": [
            {"position": 0, "color": "112244", "opacity": 1},
            {"position": 0.55, "color": "336699", "opacity": 0.75},
            {"position": 1, "color": "88CCEE", "opacity": 1},
        ],
    }


def test_slide_format_sets_solid_background_and_restores_master_inheritance(tmp_path):
    source = _generate_office_operations_sync("pptx", [
        *_seed(),
        normalize_file_patch_operation(_gradient()),
    ])["_persisted_bytes"]
    path = tmp_path / "background-reset.pptx"
    path.write_bytes(source)

    solid = _apply_office_patch_sequence_sync(str(path), _operations({
        "op": "slide.format", "slide": 1,
        "format": {"background_color": "F0A020"},
    }))
    assert not solid.get("error"), solid
    slide, background = _background_xml(solid["_persisted_bytes"])
    assert slide.follow_master_background is False
    assert background.xpath("string(./p:bgPr/a:solidFill/a:srgbClr/@val)") == "F0A020"

    solid_path = tmp_path / "solid.pptx"
    solid_path.write_bytes(solid["_persisted_bytes"])
    restored = _apply_office_patch_sequence_sync(str(solid_path), _operations({
        "op": "slide.format", "slide": 1,
        "format": {"background_color": None},
    }))
    assert not restored.get("error"), restored
    slide, background = _background_xml(restored["_persisted_bytes"])
    assert slide.follow_master_background is True
    assert background is None


@pytest.mark.parametrize(
    "invalid_format",
    [
        {},
        {"background_color": "#FFFFFF"},
        {"background_color": "FFFFFF", "background_gradient": {"angle": 0, "stops": []}},
        {"background_gradient": {"angle": 361, "stops": []}},
        {"background_gradient": {"angle": 0, "stops": [{"position": 0, "color": "000000"}]}},
        {"background_gradient": {"angle": 0, "stops": [
            {"position": 1, "color": "000000"}, {"position": 0, "color": "FFFFFF"},
        ]}},
        {"unknown": True},
    ],
)
def test_invalid_slide_format_is_atomic(tmp_path, invalid_format):
    source = _generate_office_operations_sync("pptx", _seed())["_persisted_bytes"]
    path = tmp_path / "invalid-background.pptx"
    path.write_bytes(source)
    before = path.read_bytes()
    result = _apply_office_patch_sequence_sync(str(path), _operations(
        {"op": "slide.format", "slide": 1, "format": {"background_color": "123456"}},
        {"op": "slide.format", "slide": 1, "format": invalid_format},
    ))
    assert result.get("error"), result
    assert result["operation_index"] == 1
    assert "_persisted_bytes" not in result
    assert path.read_bytes() == before
