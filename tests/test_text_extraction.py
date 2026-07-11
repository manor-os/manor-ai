"""Unit tests for text extraction service (no DB required)."""

import json
import tempfile

import pytest

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


async def test_unsupported_type(tmp_file):
    path = tmp_file(b"\x00\x01\x02binary data", ".bin")
    result = await extract_text(path)
    assert result == ""
