"""A chat PDF can be searched as text, with its attachment grant enforced."""
from __future__ import annotations

import json

import pytest
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

from packages.core.ai.tools import file_tools
from packages.core.config import get_settings
from packages.core.services import ai_file_permissions
from packages.core.services.text_extraction import extract_text


ENTITY = "01TESTCHATPDFTOOLS000000000"
PDF_PATH = "uploads/chat/0123456789abcdef.pdf"
TARGET = "Example Revenue Corporation"


def _write_pdf(path, *, long: bool = False, one_line: bool = False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)}),
    })
    suffix = b") Tj" if one_line else b") Tj T*"
    filler = [b"(Agreement filler " + b"x" * 90 + suffix for _ in range(1050)] if long else []
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 14 TL 72 720 Td " + b" ".join(filler) + b" (" + TARGET.encode() + b") Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as output:
        writer.write(output)


@pytest.fixture
def pdf_root(tmp_path, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "MANOR_FS_ENABLED", True)
    monkeypatch.setattr(settings, "MANOR_FS_ROOT", str(tmp_path))
    root = tmp_path / ENTITY
    root.mkdir()

    async def allow_access(**kwargs):
        assert kwargs["paths"] == [PDF_PATH]
        return None

    async def allow_knowledge(*_args, **_kwargs):
        return set()

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_read_access", allow_access)
    monkeypatch.setattr(file_tools, "_blocked_doc_paths", allow_knowledge)
    return root


@pytest.mark.asyncio
async def test_exact_chat_pdf_search_uses_extracted_text_past_100k(pdf_root, monkeypatch):
    _write_pdf(pdf_root / PDF_PATH, long=True)

    async def no_knowledge_acl(*_args, **_kwargs):
        raise AssertionError("chat uploads use their attachment grant, not Knowledge ACL")

    monkeypatch.setattr(file_tools, "_blocked_doc_paths", no_knowledge_acl)

    search = json.loads(await file_tools._grep_files(ENTITY, path=PDF_PATH, pattern="Example Rev"))
    assert search.get("count") == 1, search
    assert search["matches"][0]["file"] == PDF_PATH
    assert TARGET in search["matches"][0]["text"]
    assert search["files_skipped_binary"] == 0
    assert search["truncated"] is False

    read = json.loads(await file_tools._read_file(ENTITY, path=PDF_PATH, char_offset=100_000, max_chars=40_000))
    assert read["total_chars"] > 100_000
    assert TARGET in read["content"]
    assert "not an Office template" in read["editing_hint"]
    assert "full PDF" in read["editing_hint"]
    assert len(await extract_text(str(pdf_root / PDF_PATH))) == 100_000


@pytest.mark.asyncio
async def test_chat_pdf_search_grant_denial_does_not_extract(pdf_root, monkeypatch):
    _write_pdf(pdf_root / PDF_PATH)

    async def deny_access(**_kwargs):
        return {PDF_PATH}

    async def no_extract(*_args, **_kwargs):
        raise AssertionError("denied PDF bytes must not be extracted")

    monkeypatch.setattr(ai_file_permissions, "guard_ai_file_read_access", deny_access)
    monkeypatch.setattr(file_tools, "_read_supported_text", no_extract)
    denied = json.loads(await file_tools._grep_files(ENTITY, path=PDF_PATH, pattern=TARGET))
    assert denied == {"error": f"Directory not found: {PDF_PATH}"}


@pytest.mark.asyncio
async def test_pdf_search_finds_text_after_long_extracted_line(pdf_root):
    _write_pdf(pdf_root / PDF_PATH, long=True, one_line=True)
    search = json.loads(await file_tools._grep_files(ENTITY, path=PDF_PATH, pattern="Example Rev"))
    assert search["count"] == 1, search
    assert TARGET in search["matches"][0]["text"]


@pytest.mark.asyncio
async def test_directory_search_does_not_bypass_chat_attachment_grant(pdf_root):
    _write_pdf(pdf_root / PDF_PATH)
    result = json.loads(await file_tools._grep_files(ENTITY, pattern=TARGET))
    assert result.get("count") == 0, result
    assert result["files_scanned"] == 0


@pytest.mark.asyncio
async def test_exact_knowledge_pdf_still_checks_document_acl(pdf_root, monkeypatch):
    _write_pdf(pdf_root / "private.pdf")

    async def block_private(*_args, **_kwargs):
        return {"private.pdf"}

    async def no_extract(*_args, **_kwargs):
        raise AssertionError("private PDF bytes must not be extracted")

    monkeypatch.setattr(file_tools, "_blocked_doc_paths", block_private)
    monkeypatch.setattr(file_tools, "_read_supported_text", no_extract)
    result = json.loads(await file_tools._grep_files(ENTITY, path="private.pdf", pattern=TARGET))
    assert result == {"error": "Directory not found: private.pdf"}
