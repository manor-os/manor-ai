"""Unit tests for text extraction service (no DB required)."""

import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from packages.core.services import text_extraction
from packages.core.services.text_extraction import extract_text


@pytest.fixture
def tmp_file():
    """Helper that creates a temp file with given content and suffix."""
    files = []

    def _create(content: str | bytes, suffix: str) -> str:
        mode = "w" if isinstance(content, str) else "wb"
        f = tempfile.NamedTemporaryFile(mode=mode, suffix=suffix, delete=False)
        f.write(content)
        f.flush()
        f.close()
        files.append(f.name)
        return f.name

    yield _create

    import os

    for path in files:
        try:
            os.unlink(path)
        except OSError:
            pass


async def test_extract_plain_text(tmp_file):
    path = tmp_file("Hello world.\nLine two.", ".txt")
    result = await extract_text(path)
    assert "Hello world." in result
    assert "Line two." in result


async def test_extract_markdown(tmp_file):
    md = "# Title\n\nSome **bold** text and a [link](http://example.com).\n"
    path = tmp_file(md, ".md")
    result = await extract_text(path)
    # Headers should be stripped
    assert "# " not in result
    # Bold markers should be stripped
    assert "**" not in result
    # Link text should remain, URL stripped
    assert "link" in result
    assert "http://example.com" not in result


async def test_extract_html(tmp_file):
    html = (
        "<html><head><style>body{color:red}</style></head>"
        "<body><h1>Hello</h1><script>alert(1)</script>"
        "<p>World</p></body></html>"
    )
    path = tmp_file(html, ".html")
    result = await extract_text(path)
    assert "Hello" in result
    assert "World" in result
    # Tags and script content should be stripped
    assert "<h1>" not in result
    assert "alert" not in result
    assert "<style>" not in result


async def test_extract_csv(tmp_file):
    csv_content = "name,age,city\nAlice,30,NYC\nBob,25,LA\n"
    path = tmp_file(csv_content, ".csv")
    result = await extract_text(path)
    # Should be pipe-separated
    assert "name | age | city" in result
    assert "Alice | 30 | NYC" in result


async def test_extract_xlsx_reads_beyond_first_500_rows(tmp_file):
    from openpyxl import Workbook

    path = tmp_file(b"", ".xlsx")
    wb = Workbook()
    ws = wb.active
    ws.title = "验证列表"
    ws.append(["编号", "功能", "状态"])
    for index in range(1, 651):
        ws.append([index, f"功能 {index}", "待验证"])
    wb.save(path)

    result = await extract_text(path, file_type="xlsx")

    assert "[Sheet: 验证列表]" in result
    assert "501 | 功能 501 | 待验证" in result
    assert "650 | 功能 650 | 待验证" in result


async def test_extract_json(tmp_file):
    data = {"key": "value", "nested": {"a": 1}}
    path = tmp_file(json.dumps(data), ".json")
    result = await extract_text(path)
    parsed = json.loads(result)
    assert parsed["key"] == "value"
    assert parsed["nested"]["a"] == 1


async def test_extract_pdf_keeps_native_text_without_ocr(tmp_file, monkeypatch):
    path = tmp_file(b"%PDF-native", ".pdf")
    monkeypatch.setattr(text_extraction, "_extract_pdf_native", lambda _path, **_kwargs: ("Native PDF text", 1))

    def unexpected_ocr(_path, _page_count):
        raise AssertionError("OCR must not run when native PDF text exists")

    monkeypatch.setattr(text_extraction, "_ocr_pdf_sync", unexpected_ocr)

    assert await extract_text(path) == "Native PDF text"


async def test_extract_pdf_uses_ocr_when_native_text_is_blank(tmp_file, monkeypatch):
    path = tmp_file(b"%PDF-image-only", ".pdf")
    seen = {}
    monkeypatch.setattr(text_extraction, "_extract_pdf_native", lambda _path, **_kwargs: ("\n\n", 3))

    def fake_ocr(ocr_path, page_count):
        seen.update(path=ocr_path, page_count=page_count)
        return "Recognized slide text"

    monkeypatch.setattr(text_extraction, "_ocr_pdf_sync", fake_ocr)

    assert await extract_text(path) == "Recognized slide text"
    assert seen == {"path": path, "page_count": 3}


async def test_extract_pdf_preserves_native_result_when_ocr_is_unavailable(tmp_file, monkeypatch):
    path = tmp_file(b"%PDF-image-only", ".pdf")
    monkeypatch.setattr(text_extraction, "_extract_pdf_native", lambda _path, **_kwargs: ("\n\n", 3))
    monkeypatch.setattr(text_extraction, "_ocr_pdf_sync", lambda _path, _page_count: "")

    assert await extract_text(path) == "\n\n"


def test_pdf_ocr_renders_and_recognizes_each_page(monkeypatch):
    monkeypatch.setattr(
        text_extraction.shutil,
        "which",
        lambda name: f"/usr/bin/{name}" if name in {"pdftoppm", "tesseract"} else None,
    )
    calls = []

    def fake_run(command, **_kwargs):
        calls.append(command)
        if command[0].endswith("tesseract") and command[1] == "--list-langs":
            return SimpleNamespace(returncode=0, stdout="eng\nchi_sim\n", stderr="")
        if command[0].endswith("pdftoppm"):
            Path(f"{command[-1]}.png").write_bytes(b"fake png")
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        page_name = Path(command[1]).stem
        return SimpleNamespace(returncode=0, stdout=f"Text from {page_name}", stderr="")

    monkeypatch.setattr(text_extraction.subprocess, "run", fake_run)

    result = text_extraction._ocr_pdf_sync("/tmp/image-only.pdf", 2)

    assert result == "Text from page-0001\n\nText from page-0002"
    ocr_calls = [call for call in calls if call[0].endswith("tesseract") and call[1] != "--list-langs"]
    assert len(ocr_calls) == 2
    assert all(call[-2:] == ["-l", "eng+chi_sim"] for call in ocr_calls)


async def test_unsupported_type(tmp_file):
    path = tmp_file(b"\x00\x01\x02binary data", ".bin")
    result = await extract_text(path)
    assert result == ""
