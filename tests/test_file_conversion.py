"""Native conversion keeps original bytes, authorization and Knowledge atomicity."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pypdf import PdfReader, PdfWriter
from sqlalchemy import select

from packages.core.ai.runtime import file_actions, generated_files
from packages.core.ai.runtime.file_contracts import FileApprovalOperationFactory
from packages.core.ai.tools import file_tools
from packages.core.ai.tools.generate_file import document, tool
from packages.core.contracts.file_engine import FILE_CONVERSION_TARGETS
from packages.core.models.base import generate_ulid
from packages.core.models.event import EventLog
from packages.core.services import file_conversion
from packages.core.services.office_editing import OfficeConverterUnavailableError
from tests.test_knowledge_file_consistency import _doc_for, _seed_workspace, fs_enabled  # noqa: F401


def pdf_bytes():
    output = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=595, height=842)
    writer.write(output)
    return output.getvalue()


def source_bytes(extension="docx"):
    output = io.BytesIO()
    if extension == "docx":
        from docx import Document
        from docx.shared import Inches, Pt

        office = Document()
        section = office.sections[0]
        section.page_width, section.page_height = Inches(8.5), Inches(11)
        section.left_margin = Inches(1)
        section.header.paragraphs[0].text = "Conversion letterhead"
        section.footer.paragraphs[0].text = "Conversion footer"
        title = office.add_paragraph().add_run("Original agreement")
        title.bold, title.font.size = True, Pt(24)
        table = office.add_table(rows=2, cols=2)
        table.cell(0, 0).text, table.cell(0, 1).text = "Service", "Amount"
        table.cell(1, 0).text, table.cell(1, 1).text = "Retainer", "1200"
        office.add_page_break()
        office.add_paragraph("Second page terms")
    elif extension == "pptx":
        from pptx import Presentation
        from pptx.util import Inches

        office = Presentation()
        for text in ("Original agreement", "Second page terms"):
            slide = office.slides.add_slide(office.slide_layouts[6])
            slide.shapes.add_textbox(Inches(1), Inches(1), Inches(6), Inches(2)).text = text
    else:
        from openpyxl import Workbook

        office = Workbook()
        office.active["A1"] = "Original agreement"
        office.active["A2"] = 1200
        office.active.column_dimensions["A"].width = 35
    office.save(output)
    if extension == "xlsx":
        office.close()
    return output.getvalue()


def test_conversion_is_discoverable_without_a_new_tool_or_skill_runtime():
    info = file_tools._file_engine_capabilities()
    assert "convert" in info["operations"]
    assert info["conversion"]["targets"] == FILE_CONVERSION_TARGETS
    assert info["conversion"]["tool"] == "generate_file"
    assert "source" in tool.GENERATE_FILE_SCHEMA["function"]["parameters"]["properties"]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind,nested", [("document", False), ("document", True), ("pdf", False)])
async def test_conversion_routes_directly_to_document_runtime(monkeypatch, kind, nested):
    runtime = AsyncMock(return_value='{"converted":true}')
    monkeypatch.setattr(document, "runtime_generate_document_file", runtime)
    args = {"source": {"path": "source.docx", "expected_sha256": "a" * 64}, "file_type": "pdf"}
    result = json.loads(await tool._generate_file_handler(
        "entity", kind=kind, name="copy.pdf", **({"params": args} if nested else args),
    ))
    assert result["converted"]
    assert runtime.call_args.kwargs["source"] == args["source"]
    assert runtime.call_args.kwargs["content"] is None
    assert runtime.call_args.kwargs["file_type"] == "pdf"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"source": None}, {"kind": "image"}, {"kind": "code"}, {"kind": "audio"},
    {"content": "reconstructed text"}, {"prompt": "rebuild it"}, {"operations": []},
    {"template": {}}, {"options": {}}, {"expected_sha256": "b" * 64},
    {"params": {"source": {"path": "other.docx", "expected_sha256": "b" * 64}}},
    {"params": {"file_type": "docx"}}, {"name": ""},
])
async def test_conflicting_conversion_never_invokes_a_generator(monkeypatch, extra):
    unexpected = AsyncMock(side_effect=AssertionError("Invalid conversion reached generator"))
    for handler in ("handle_document", "handle_image", "handle_code", "handle_audio"):
        monkeypatch.setattr(tool, handler, unexpected)
    result = json.loads(await tool._generate_file_handler("entity", **{
        "kind": "document", "source": {"path": "source.docx", "expected_sha256": "a" * 64},
        "name": "copy.pdf", "file_type": "pdf", **extra,
    }))
    assert result.get("error"), result


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [
    {}, {"path": "source.docx"}, {"path": "source.docx", "expected_sha256": "invalid"},
    {"path": "source.docx", "expected_sha256": "a" * 64, "extra": True},
    *[{"path": path, "expected_sha256": "a" * 64} for path in (
        "", "../source.docx", "/source.docx", "..\\source.docx", ".ai/source.docx",
        "nested/../source.docx", "source\x00.docx", "source.pdf", "source.txt",
    )],
])
async def test_invalid_source_or_pair_fails_before_authorization(tmp_path, monkeypatch, source):
    monkeypatch.setattr(file_actions, "runtime_entity_file_root", lambda _entity: str(tmp_path))
    unexpected = AsyncMock(side_effect=AssertionError("Invalid conversion reached authorization"))
    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", unexpected)
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", unexpected)
    result = json.loads(await generated_files.runtime_generate_document_file(
        entity_id="entity", user_id="", conversation_id="", name="copy.pdf", file_type="pdf", source=source,
    ))
    assert result.get("error"), result


@pytest.mark.asyncio
@pytest.mark.usefixtures("fs_enabled")
@pytest.mark.parametrize("outcome", [
    "success", "existing", "read_denied", "stale", "approval_denied", "converter_unavailable",
    "timeout", "projection_failure", "snapshot_changed_after_read",
    pytest.param(
        "real_conversion",
        marks=[
            pytest.mark.skipif(
                not (shutil.which("soffice") or shutil.which("libreoffice")),
                reason="LibreOffice not installed",
            ),
            pytest.mark.media_integration,
            pytest.mark.oss_regression,
        ],
    ),
])
async def test_conversion_knowledge_transaction(db_session, monkeypatch, outcome):
    from packages.core.services.knowledge_sync import sync_file_to_knowledge

    entity_id, workspace_id = generate_ulid(), generate_ulid()
    await _seed_workspace(db_session, entity_id, workspace_id)
    root = Path(file_tools._get_entity_root(entity_id))
    original_bytes = source_bytes()
    original_path = root / "original.docx"
    original_path.write_bytes(original_bytes)
    sync = await sync_file_to_knowledge(entity_id=entity_id, abs_path=str(original_path), entity_root=str(root), source="upload", created_by="test")
    original_doc = await _doc_for(db_session, entity_id, original_path.name)
    original_id, original_meta = original_doc.id, copy.deepcopy(original_doc.metadata_)
    assert sync.document_id == original_id
    original_events = set((await db_session.scalars(select(EventLog.id).where(EventLog.entity_id == entity_id))).all())
    source = {"path": original_path.name, "expected_sha256": hashlib.sha256(original_bytes).hexdigest()}
    if outcome == "stale":
        source["expected_sha256"] = "a" * 64
    target_rel = await file_tools._workspace_scoped_new_file_path(entity_id=entity_id, entity_root=str(root), workspace_id=workspace_id, path="copy.pdf")
    target = root / target_rel
    if outcome == "existing":
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"Existing output")

    async def approve(**_kwargs):
        if outcome == "approval_denied":
            return '{"error":"approval_required"}'
        if outcome == "snapshot_changed_after_read":
            original_path.write_bytes(b"External writer changed the source")

    read_guard = AsyncMock(return_value='{"error":"file_permission_denied"}' if outcome == "read_denied" else None)
    mutation_guard = AsyncMock(side_effect=approve)
    monkeypatch.setattr(file_actions, "runtime_guard_file_read_access", read_guard)
    monkeypatch.setattr(file_actions, "runtime_guard_file_mutation", mutation_guard)
    converted = pdf_bytes()
    real_converter = file_conversion.convert_office_file

    async def convert(data, source_type, target_type):
        nonlocal converted
        assert data == original_bytes and source_type == "docx" and target_type == "pdf"
        if outcome == "converter_unavailable":
            raise OfficeConverterUnavailableError()
        if outcome == "timeout":
            raise subprocess.TimeoutExpired("soffice", 120)
        if outcome == "real_conversion":
            converted = await real_converter(data, source_type, target_type)
            assert "Original agreement" in PdfReader(io.BytesIO(converted)).pages[0].extract_text()
        return converted

    converter = AsyncMock(side_effect=convert)
    monkeypatch.setattr(file_conversion, "convert_office_file", converter)
    if outcome == "projection_failure":
        monkeypatch.setattr(generated_files, "runtime_generated_file_metadata", AsyncMock(side_effect=OSError("injected projection failure")))
    result = json.loads(await tool._generate_file_handler(
        entity_id, kind="document", name="copy.pdf", file_type="pdf", source=source, workspace_id=workspace_id,
    ))
    reloaded = await _doc_for(db_session, entity_id, original_path.name)
    assert reloaded.id == original_id and reloaded.metadata_ == original_meta
    if outcome != "snapshot_changed_after_read":
        assert original_path.read_bytes() == original_bytes
    if mutation_guard.called:
        args = mutation_guard.call_args.kwargs
        assert args["paths"] == [target_rel]
        assert args["approval_payload"] == {"file_type": "pdf", "source": source}
        assert json.loads(args["content_preview"]) == args["approval_payload"]
    if outcome in {"success", "snapshot_changed_after_read", "real_conversion"}:
        assert result.get("created") and result.get("converted"), result
        output = await _doc_for(db_session, entity_id, "copy.pdf")
        assert output.id != original_id
        assert output.fs_path == target_rel
        assert output.metadata_["origin"]["workspace_id"] == workspace_id
        assert result["document"]["document_id"] == output.id
        assert result["document"]["source_sha256"] == hashlib.sha256(converted).hexdigest()
        assert target.read_bytes() == converted
    else:
        assert result.get("error"), result
        if outcome in {"existing", "stale", "read_denied", "approval_denied"}:
            converter.assert_not_called()
        if outcome == "existing":
            assert target.read_bytes() == b"Existing output"
        else:
            assert not target.exists()
        assert await _doc_for(db_session, entity_id, "copy.pdf") is None
        assert set((await db_session.scalars(select(EventLog.id).where(EventLog.entity_id == entity_id))).all()) == original_events


@pytest.mark.parametrize("field,value", [("path", "other.docx"), ("expected_sha256", "b" * 64)])
def test_approval_is_bound_to_exact_conversion_source(field, value):
    payload = {"file_type": "pdf", "source": {"path": "source.docx", "expected_sha256": "a" * 64}}
    approved = FileApprovalOperationFactory.create(tool_name="generate_file", action="create_document", paths=["copy.pdf"], payload=payload)
    changed = copy.deepcopy(payload)
    changed["source"][field] = value
    retry = FileApprovalOperationFactory.create(tool_name="generate_file", action="create_document", paths=["copy.pdf"], payload=changed)
    assert not retry.matches(approved.to_record())


def test_conversion_input_is_bounded_before_starting_office(monkeypatch):
    from packages.core.services.office_editing import OfficeConversionLimitError

    monkeypatch.setattr(file_conversion, "FILE_CONVERSION_MAX_INPUT_BYTES", 10)
    monkeypatch.setattr(file_conversion.shutil, "which", lambda _name: pytest.fail("Oversized source reached converter"))
    for data in (b"", b"a" * 11):
        with pytest.raises(OfficeConversionLimitError):
            file_conversion._convert_office_file_sync(data, "docx", "pdf")


@pytest.mark.usefixtures("fs_enabled")
def test_conversion_source_snapshot_obeys_byte_limit():
    from packages.core.services.entity_fs import provision_entity_filesystem
    from packages.core.services.office_editing import OfficeConversionLimitError

    entity_id = generate_ulid()
    provision_entity_filesystem(entity_id)
    root = Path(file_tools._get_entity_root(entity_id))
    data = b"source bytes"
    (root / "original.docx").write_bytes(data)
    source = {"path": "original.docx", "expected_sha256": hashlib.sha256(data).hexdigest()}
    with pytest.raises(OfficeConversionLimitError):
        generated_files._read_office_template_sync(entity_id, source, max_bytes=10)
    assert generated_files._read_office_template_sync(entity_id, source, max_bytes=len(data)) == data


@pytest.mark.parametrize("outcome", ["missing", "timeout", "no_output", "invalid_pdf", "empty", "oversized", "failure"])
def test_converter_fails_closed_and_removes_temporary_files(tmp_path, monkeypatch, outcome):
    temporary = []
    monkeypatch.setattr(file_conversion.shutil, "which", lambda _name: None if outcome == "missing" else "/usr/bin/soffice")

    def run(args, **kwargs):
        root = Path(args[args.index("--outdir") + 1])
        temporary.append(root)
        assert kwargs["timeout"] == 120
        profile = (root / "profile/user/registrymodifications.xcu").read_text()
        assert "DisableMacrosExecution" in profile
        if outcome == "timeout":
            raise subprocess.TimeoutExpired(args, 120)
        if outcome == "invalid_pdf":
            (root / "source.pdf").write_bytes(b"not a PDF")
        elif outcome == "empty":
            (root / "source.pdf").write_bytes(b"")
        elif outcome == "oversized":
            (root / "source.pdf").write_bytes(b"a" * 11)
        return SimpleNamespace(returncode=1 if outcome == "failure" else 0)

    monkeypatch.setattr(file_conversion, "run_in_process_group", run)
    monkeypatch.setattr(file_conversion, "FILE_CONVERSION_MAX_OUTPUT_BYTES", 10)
    with pytest.raises(Exception):
        file_conversion._convert_office_file_sync(b"source bytes", "docx", "pdf")
    assert all(not root.exists() for root in temporary)


@pytest.mark.skipif(not (shutil.which("soffice") or shutil.which("libreoffice")), reason="LibreOffice not installed")
@pytest.mark.parametrize(
    "extension",
    [
        pytest.param(extension, marks=[pytest.mark.media_integration, pytest.mark.oss_regression])
        for extension in ("docx", "pptx", "xlsx")
    ],
)
def test_real_office_pdf_conversion_preserves_pages_and_content(extension):
    original = source_bytes(extension)
    original_hash = hashlib.sha256(original).hexdigest()
    converted = file_conversion._convert_office_file_sync(original, extension, "pdf")
    reader = PdfReader(io.BytesIO(converted))
    text = "\n".join(page.extract_text() for page in reader.pages)
    assert "Original agreement" in text
    assert hashlib.sha256(original).hexdigest() == original_hash
    if extension in {"docx", "pptx"}:
        assert len(reader.pages) == 2
        assert "Second page terms" in reader.pages[1].extract_text()
    if extension == "docx":
        assert float(reader.pages[0].mediabox.width) == pytest.approx(612, abs=1)
        assert float(reader.pages[0].mediabox.height) == pytest.approx(792, abs=1)
        for page in reader.pages:
            assert "Conversion letterhead" in page.extract_text()
            assert "Conversion footer" in page.extract_text()
        assert "Retainer" in text and "1200" in text
