"""Fixed publication geometry rejects density it cannot safely contain."""
from __future__ import annotations

import json
import hashlib
import io
from pathlib import Path
from zipfile import ZipFile

import pytest

from packages.core.ai.tools import creative_studio_tools as studio


def _pages(*, title="社区摄影活动", bullets=None):
    return [{"title": title, "bullets": bullets or ["出发前检查设备。", "拍摄前征得对方同意。"],
             "notes": "测试内容。"} for _ in range(3)]


@pytest.mark.parametrize("pages, label, field", [
    (_pages(title="中" * 54, bullets=["文" * 90] * 4), "中文测试", "Page 1 title"),
    (_pages(bullets=["文" * 90] * 4), "中文测试", "Page 1 body"),
    (_pages(title="标题\n\n\n标题"), "中文测试", "Page 1 title"),
    (_pages(bullets=["要点\n" * 8]), "中文测试", "Page 1 body"),
    (_pages(), "标签\n标签", "Page 1 label"),
    (_pages(title="标题\u2028\u2028\u2028标题"), "中文测试", "Page 1 title"),
])
async def test_dense_publication_fails_before_any_file_access(monkeypatch, pages, label, field):
    async def unexpected(*args, **kwargs):
        pytest.fail("Dense content must be rejected before reading or writing files")

    monkeypatch.setattr(studio, "_source_path", unexpected)
    monkeypatch.setattr(studio, "_scope", unexpected)
    monkeypatch.setattr(studio, "_save", unexpected)
    result = json.loads(await studio._compose_creative_publication_handler(
        entity_id="entity", workspace_id="workspace", output_directory="production/run",
        label=label, theme="sage", illustration_path="illustration.png", source_notes="Test source",
        pages=pages,
    ))
    assert result["status"] == "failed", result
    assert "layout-density" in result["error"] and field in result["error"] and "Shorten" in result["error"]
    with pytest.raises(ValueError, match=field):
        studio._publication_operations(pages, label=label, theme="sage", illustration={})


@pytest.mark.parametrize("pages, label", [
    (_pages(), "中文测试"),
    (_pages(title="Community photo walk", bullets=["Check your equipment before leaving.",
                                                 "Ask before taking a close-up portrait."]), "Photo club"),
])
def test_short_chinese_and_english_keep_the_native_layout(pages, label):
    from packages.core.ai.runtime.generated_files import _generate_office_operations_sync
    from packages.core.contracts.file_engine import normalize_file_patch_operation

    fixture = Path(__file__).resolve().parents[1] / "apps/web/public/blueprints/creative/carousel-page-01.jpg"
    picture = fixture.read_bytes()
    digest = hashlib.sha256(picture).hexdigest()
    operations = studio._publication_operations(pages, label=label, theme="sage",
        illustration={"path": "fixture.jpg", "expected_sha256": digest})
    # Each page keeps its own layout's geometry and type sizes, unshrunk.
    plans = [studio._publication_page_plan(page, index=index, count=len(pages), label=label, accent="000000")
             for index, page in enumerate(pages)]
    planned_title = next(item for item in plans[0] if item[0] == "text" and item[1] == "title")
    title = next(op for op in operations if op["op"] == "textbox.insert" and op["text"] == pages[0]["title"])
    assert title["transform"] == dict(zip(("x", "y", "width", "height"), planned_title[3:7]))
    sizes = {op["format"]["font_size"] for op in operations if op["op"] == "paragraph.format"}
    assert sizes == {item[7] for plan in plans for item in plan if item[0] == "text"}
    result = _generate_office_operations_sync("pptx", [normalize_file_patch_operation(op) for op in operations],
                                            resources={("fixture.jpg", digest): picture})
    assert not result.get("error"), result
    with ZipFile(io.BytesIO(result["_persisted_bytes"])) as archive:
        for index, page in enumerate(pages, 1):
            slide = archive.read(f"ppt/slides/slide{index}.xml").decode()
            assert page["title"] in slide
            assert all(bullet in slide for bullet in page["bullets"])


def test_density_estimate_retains_explicit_blank_lines_and_word_wrapping():
    assert studio._publication_text_lines("甲\n\n乙\n", size=21, width=500) == (4, 4)
    assert studio._publication_text_lines("hello world", size=21, width=100) == (2, 1)
    assert studio._publication_text_lines("e\u0301", size=21, width=100) == (1, 1)
    with pytest.raises(ValueError, match="control"):
        studio._validate_publication_layout(_pages(title="标题\t标题"), label="测试")
    with pytest.raises(ValueError, match="control"):
        studio._validate_publication_layout(_pages(title="\t标题"), label="测试")
