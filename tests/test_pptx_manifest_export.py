"""Project manifests must become real native objects before PPTX delivery."""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

SCRIPTS = Path(__file__).resolve().parents[1] / "packages/core/ai/skills/pptx/scripts"
sys.path.insert(0, str(SCRIPTS))
from pptx_quality_gate import validate_relationships  # noqa: E402


def _project(tmp_path: Path) -> Path:
    project = tmp_path / "deck"
    (project / "svg_output").mkdir(parents=True)
    (project / "notes").mkdir()
    (project / "notes" / "P01.md").write_text("Synthetic export verification.")
    (project / "svg_output" / "P01.svg").write_text(
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1280 720">'
        '<rect width="1280" height="720" fill="#ffffff"/>'
        '<rect x="100" y="200" width="650" height="400" fill="#ffffff" data-native-chart-slot="evidence-chart"/>'
        '<rect x="800" y="200" width="400" height="400" fill="#ffffff" data-video-slot="demo-video"/>'
        '</svg>', encoding="utf-8",
    )
    return project


def _charts(project: Path, *, slide: str = "P01") -> None:
    (project / "native_charts.json").write_text(json.dumps({
        "version": 1, "slides": {slide: [{"id": "evidence-chart", "slot": "evidence-chart",
            "type": "column", "categories": ["Before", "After"],
            "series": [{"name": "Synthetic values", "values": [46, 18]}]}]},
    }))


def _video(project: Path, source: str) -> None:
    (project / "embedded_media.json").write_text(json.dumps({
        "version": 1, "slides": {"P01": [{"id": "demo-video", "slot": "demo-video",
            "type": "video", "source": source, "poster": "media/poster.png", "fit": "contain", "play": "on_click"}]},
    }))


def _export(project: Path, output: Path):
    return subprocess.run([sys.executable, str(SCRIPTS / "svg_to_pptx.py"), str(project),
                           "-o", str(output), "--quiet"], capture_output=True, text=True, timeout=60)


def test_native_chart_manifest_exports_real_editable_chart_and_valid_notes(tmp_path: Path):
    project = _project(tmp_path)
    _charts(project)
    output = tmp_path / "export.pptx"
    result = _export(project, output)
    assert result.returncode == 0, result.stdout + result.stderr
    charts = [shape.chart for shape in Presentation(output).slides[0].shapes
              if shape.shape_type == MSO_SHAPE_TYPE.CHART]
    assert len(charts) == 1
    assert list(charts[0].series[0].values) == [46, 18]
    assert validate_relationships(output) == []


def test_invalid_chart_manifest_cannot_replace_existing_output(tmp_path: Path):
    project = _project(tmp_path)
    _charts(project, slide="P09")
    output = tmp_path / "export.pptx"
    output.write_bytes(b"previous output")
    result = _export(project, output)
    assert result.returncode != 0
    assert "Unknown slide key" in result.stderr
    assert output.read_bytes() == b"previous output"


@pytest.mark.media_integration
@pytest.mark.oss_regression
def test_chart_and_authorized_video_manifests_export_native_objects_together(tmp_path: Path):
    assert shutil.which("ffmpeg") and shutil.which("ffprobe"), "Real FFmpeg is required for native video export verification"
    project = _project(tmp_path)
    media = project / "media"
    media.mkdir()
    Image.new("RGB", (64, 64), "teal").save(media / "poster.png")
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
                    "color=c=teal:s=64x64:d=0.2", "-c:v", "mpeg4", str(media / "demo.mp4")],
                   check=True, capture_output=True, timeout=30)
    _charts(project)
    _video(project, "media/demo.mp4")
    output = tmp_path / "export.pptx"
    result = _export(project, output)
    assert result.returncode == 0, result.stdout + result.stderr
    types = [shape.shape_type for shape in Presentation(output).slides[0].shapes]
    assert types.count(MSO_SHAPE_TYPE.CHART) == 1
    assert types.count(MSO_SHAPE_TYPE.MEDIA) == 1
    assert validate_relationships(output) == []


@pytest.mark.parametrize("via_symlink", [False, True])
def test_media_manifest_rejects_sources_outside_project(tmp_path: Path, via_symlink: bool):
    project = _project(tmp_path)
    outside = tmp_path / "private.mp4"
    outside.write_bytes(b"private video")
    if via_symlink:
        link = project / "foreign.mp4"
        link.symlink_to(outside)
        source = "foreign.mp4"
    else:
        source = str(outside)
    _video(project, source)
    output = tmp_path / "export.pptx"
    result = _export(project, output)
    assert result.returncode != 0
    assert "must stay inside the current project" in result.stderr
    assert not output.exists()
