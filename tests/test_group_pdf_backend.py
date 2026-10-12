"""Group PDF output keeps both English and Chinese text readable."""
from __future__ import annotations

import io

import pytest
from pypdf import PdfReader

from packages.core.services.room_file_mutation_tools import _render_file


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "expected"),
    [
        ("# Summary\nGroup only", "Group only"),
        ("# 群聊摘要\n**你好世界**", "你好世界"),
    ],
)
async def test_group_pdf_contains_readable_text(content: str, expected: str) -> None:
    output, mime = await _render_file("summary.pdf", content)

    assert mime == "application/pdf"
    assert output.startswith(b"%PDF-")
    extracted = "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(output)).pages)
    assert expected in extracted
