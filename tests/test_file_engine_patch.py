from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from packages.core.ai.tools import file_tools


def _tool_names() -> set[str]:
    return {schema["function"]["name"] for schema, _handler in file_tools.get_tools()}


def test_file_engine_has_one_native_tool_surface() -> None:
    from pathlib import Path

    from packages.core.ai.mcp import get_module
    from packages.core.ai.tool_pool import ToolPool
    from packages.core.ai.tools.mcp_builtin import _SERVER_TOOL_SCHEMAS
    from packages.core.services.mcp_seed import _MCP_CATALOG

    pool = ToolPool()
    pool.initialize()
    registered = set(pool.registered_tool_names())
    assert {"generate_file", "read_file", "inspect_file_engine", "patch_file"} <= registered
    assert not {"write_file", "edit_file", "generate_document_file"} & registered
    assert not any(name.startswith("mcp__manor_mcp_file_engine__") for name in registered)
    assert get_module("manor_mcp_file_engine") is None
    assert "manor_mcp_file_engine" not in _SERVER_TOOL_SCHEMAS
    assert "manor_mcp_file_engine" not in {row[0] for row in _MCP_CATALOG}
    config = json.loads(Path("packages/core/ai/skills/file_engine/config.json").read_text())
    assert set(config["tools"]) == {
        "generate_file", "patch_file", "inspect_file_engine",
         "read_file", "search_documents", "list_documents",
    }


def test_inspect_file_engine_reports_all_generate_file_kinds() -> None:
    from packages.core.contracts.audio_generation import GenerateFileKind

    payload = file_tools._file_engine_capabilities()

    assert set(payload["generate_kinds"]) == set(GenerateFileKind.values())
    assert payload["limits"]["multi_operation_per_call"] is True
    assert {
        "diagram",
        "code",
        "document",
        "word_document",
        "pdf",
        "presentation",
        "spreadsheet",
        "image",
        "video",
        "audio",
    } <= set(payload["generate_kinds"])


@pytest.mark.asyncio
async def test_agent_tool_picker_retires_old_file_entries(db_session) -> None:
    from packages.core.models.workspace import ToolDefinition
    from packages.core.services.agent_service import list_tool_definitions

    retired = [
        ToolDefinition(name=name, schema={}, status="active")
        for name in (
            "write_file", "edit_file", "generate_document_file",
            "mcp__manor_mcp_file_engine__inspect",
            "mcp__manor_mcp_file_engine__generate",
            "mcp__manor_mcp_file_engine__patch",
        )
    ]
    db_session.add_all(retired)
    await db_session.flush()

    listed = await list_tool_definitions(db_session)

    assert {"generate_file", "patch_file"} <= {tool.name for tool in listed}
    assert not {tool.name for tool in retired} & {tool.name for tool in listed}
    assert all(tool.status == "inactive" for tool in retired)
    # Historical references remain readable; only the callable catalog retires.
    for tool in retired:
        assert await db_session.get(ToolDefinition, tool.id) is not None


@pytest.mark.parametrize(
    ("file_type", "can_patch", "operations"),
    [
        ("txt", True, {"replace_text", "text.replace"}),
        ("md", True, {"replace_text", "text.replace"}),
        ("json", True, {"replace_text", "text.replace", "json.add", "json.replace", "json.remove"}),
        ("diagram.json", True, {"replace_text", "text.replace", "json.add", "json.replace", "json.remove"}),
        ("html", True, {"replace_text", "text.replace"}),
        ("csv", True, {"replace_text", "text.replace", "set_cell", "cell.set", "append_row", "row.append"}),
        ("docx", True, {"replace_text", "text.replace", "text.set", "paragraph.insert", "paragraph.delete", "paragraph.format", "paragraph_style.insert", "paragraph_style.format", "paragraph_style.delete", "section.insert", "page.setup", "textbox.insert", "shape.transform", "shape.format", "shape.delete", "table.insert", "table.format", "table.delete", "set_cell", "cell.set", "cell.format", "picture.insert", "picture.delete", "picture.replace", "picture.format"}),
        ("pptx", True, {"replace_text", "text.replace", "text.set", "paragraph.insert", "paragraph.delete", "paragraph.format", "page.setup", "shape.transform", "shape.reorder", "shape.insert", "shape.delete", "shape.format", "shape.group", "shape.ungroup", "picture.insert", "picture.delete", "picture.replace", "picture.format", "chart.insert", "chart.delete", "chart.data", "chart.format", "slide.insert", "slide.duplicate", "slide.delete", "slide.format", "slide.reorder", "textbox.insert", "table.insert", "table.format", "table.delete", "set_cell", "cell.set", "cell.format"}),
        ("xlsx", True, {"set_cell", "cell.set", "update_row", "row.update", "append_row", "row.append", "add_sheet", "sheet.add", "sheet.delete", "sheet.reorder", "sheet.rename", "cell.format", "sheet.format", "merge.set", "merge.clear", "validation.insert", "validation.format", "validation.delete", "conditional_format.insert", "conditional_format.format", "conditional_format.delete", "page.setup", "table.insert", "table.format", "table.delete", "picture.insert", "picture.delete", "picture.replace", "picture.format", "chart.insert", "chart.delete", "chart.data", "chart.format"}),
        ("xlsm", True, {"set_cell", "cell.set", "update_row", "row.update", "append_row", "row.append", "add_sheet", "sheet.add", "sheet.delete", "sheet.reorder", "sheet.rename", "cell.format", "sheet.format", "merge.set", "merge.clear", "validation.insert", "validation.format", "validation.delete", "conditional_format.insert", "conditional_format.format", "conditional_format.delete", "page.setup", "table.insert", "table.format", "table.delete", "picture.insert", "picture.delete", "picture.replace", "picture.format", "chart.insert", "chart.delete", "chart.data", "chart.format"}),
        ("pdf", True, {"page.rotate"}),
        ("doc", False, set()),
        ("ppt", False, set()),
        ("xls", False, set()),
    ],
)
def test_inspect_file_engine_reports_patch_contract_for_every_user_file_type(
    file_type: str,
    can_patch: bool,
    operations: set[str],
) -> None:
    payload = file_tools._file_engine_capabilities(file_type)["selected"]

    assert payload["can_patch"] is can_patch
    assert set(payload["operations"]) == operations
    assert payload["can_generate_from_operations"] is (file_type in {"docx", "pptx", "xlsx"})
    assert payload["can_generate_from_template"] is (
        file_type in {"docx", "pptx", "xlsx", "xlsm"}
    )
    if file_type == "xlsm":
        assert payload["can_generate"] is True
        assert payload["generate_kind"] == "spreadsheet"


@pytest.mark.asyncio
async def test_patch_file_applies_text_batch_once(tmp_path, monkeypatch) -> None:
    entity_id = "entity-1"
    root = tmp_path / entity_id
    root.mkdir()
    rel_path = "docs/example.md"
    path = root / rel_path
    path.parent.mkdir()
    path.write_text("alpha old beta stale", encoding="utf-8")

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))

    async def allow_file_access(**_kwargs):
        return None

    async def fake_guard_expected_source_sha(**_kwargs):
        return file_tools.ExpectedSourceGuardResult()

    commits: list[bytes] = []

    async def fake_commit_file_projection(**kwargs):
        commits.append(kwargs["data"])
        target = root / kwargs["rel_path"]
        target.write_bytes(kwargs["data"])
        return (
            str(target),
            SimpleNamespace(synced=True, document_id="doc-1", reason=None),
            {"size": len(kwargs["data"]), "mtime_ns": 1, "source_sha256": "new-sha"},
        )

    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(file_tools, "_guard_expected_source_sha", fake_guard_expected_source_sha)
    monkeypatch.setattr(file_tools, "_commit_file_projection", fake_commit_file_projection)

    payload = json.loads(await file_tools._patch_file(
        entity_id,
        path=rel_path,
        operations=[
            {"op": "text.replace", "old_text": "old", "new_text": "new"},
            {"op": "text.replace", "old_text": "stale", "new_text": "fresh"},
        ],
    ))

    assert payload["patched"] is True
    assert payload["operations_applied"] == 2
    assert payload["replacements"] == 2
    assert path.read_text(encoding="utf-8") == "alpha new beta fresh"
    assert len(commits) == 1


@pytest.mark.asyncio
async def test_patch_file_rejects_text_batch_without_writing_on_late_failure(tmp_path, monkeypatch) -> None:
    entity_id = "entity-1"
    root = tmp_path / entity_id
    root.mkdir()
    rel_path = "docs/example.md"
    path = root / rel_path
    path.parent.mkdir()
    original = "alpha old beta"
    path.write_text(original, encoding="utf-8")

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))

    async def allow_file_access(**_kwargs):
        return None

    async def fake_guard_expected_source_sha(**_kwargs):
        return file_tools.ExpectedSourceGuardResult()

    async def fail_commit_file_projection(**_kwargs):
        raise AssertionError("failed batch must not commit")

    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(file_tools, "_guard_expected_source_sha", fake_guard_expected_source_sha)
    monkeypatch.setattr(file_tools, "_commit_file_projection", fail_commit_file_projection)

    payload = json.loads(await file_tools._patch_file(
        entity_id,
        path=rel_path,
        operations=[
            {"op": "text.replace", "old_text": "a", "new_text": "b"},
            {"op": "text.replace", "old_text": "missing", "new_text": "fresh"},
        ],
    ))

    assert payload["error"] == "old_text not found in file. Ensure exact match including whitespace."
    assert payload["operation_index"] == 1
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.asyncio
@pytest.mark.parametrize("extension", ["txt", "md", "json", "html", "csv"])
async def test_patch_file_rewrites_text_like_files_end_to_end(tmp_path, monkeypatch, extension) -> None:
    entity_id = "entity-1"
    root = tmp_path / entity_id
    root.mkdir()
    rel_path = f"docs/example.{extension}"
    path = root / rel_path
    path.parent.mkdir()
    path.write_text("alpha old omega", encoding="utf-8")

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))
    async def allow_file_access(**_kwargs):
        return None

    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)

    async def fake_guard_expected_source_sha(**_kwargs):
        return file_tools.ExpectedSourceGuardResult()

    async def fake_commit_file_projection(**kwargs):
        target = root / kwargs["rel_path"]
        target.write_bytes(kwargs["data"])
        return (
            str(target),
            SimpleNamespace(synced=True, document_id="doc-1", reason=None),
            {"size": len(kwargs["data"]), "mtime_ns": 1, "source_sha256": "new-sha"},
        )

    monkeypatch.setattr(file_tools, "_guard_expected_source_sha", fake_guard_expected_source_sha)
    monkeypatch.setattr(file_tools, "_commit_file_projection", fake_commit_file_projection)

    payload = json.loads(await file_tools._patch_file(
        entity_id,
        path=rel_path,
        operations=[{"op": "text.replace", "old_text": "old", "new_text": "new"}],
        user_id="user-1",
    ))

    assert payload["edited"] is True
    assert payload["path"] == rel_path
    assert path.read_text(encoding="utf-8") == "alpha new omega"
    assert payload["knowledge_synced"] is True


def test_docx_patch_rewrites_real_document_package(tmp_path) -> None:
    from docx import Document

    path = tmp_path / "proposal.docx"
    doc = Document()
    doc.add_paragraph("Hello Old Title")
    doc.save(path)

    result = file_tools._replace_docx_sync(str(path), "Old Title", "New Title", False)
    assert result["edited"] is True
    assert result["file_type"] == "docx"

    out = tmp_path / "proposal-edited.docx"
    out.write_bytes(result["_persisted_bytes"])
    edited = Document(out)
    assert "Hello New Title" in "\n".join(p.text for p in edited.paragraphs)


def test_docx_batch_patch_rewrites_real_document_package_once(tmp_path) -> None:
    from docx import Document

    path = tmp_path / "proposal.docx"
    doc = Document()
    doc.add_paragraph("Hello Old Title")
    doc.add_paragraph("Status: Draft")
    doc.save(path)

    result = file_tools._apply_office_patch_sequence_sync(
        str(path),
        [
            {"operation": "replace_text", "old_text": "Old Title", "new_text": "New Title"},
            {"operation": "replace_text", "old_text": "Draft", "new_text": "Final"},
        ],
    )
    assert result["patched"] is True
    assert result["file_type"] == "docx"
    assert result["operations_applied"] == 2
    assert result["replacements"] == 2

    out = tmp_path / "proposal-edited.docx"
    out.write_bytes(result["_persisted_bytes"])
    edited = Document(out)
    text = "\n".join(p.text for p in edited.paragraphs)
    assert "Hello New Title" in text
    assert "Status: Final" in text


def test_pptx_patch_rewrites_real_presentation_package(tmp_path) -> None:
    from pptx import Presentation

    path = tmp_path / "deck.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Old Roadmap"
    deck.save(path)

    result = file_tools._replace_pptx_sync(str(path), "Old Roadmap", "New Roadmap", False)
    assert result["edited"] is True
    assert result["file_type"] == "pptx"

    out = tmp_path / "deck-edited.pptx"
    out.write_bytes(result["_persisted_bytes"])
    edited = Presentation(out)
    assert edited.slides[0].shapes.title.text == "New Roadmap"


def test_pptx_batch_patch_rewrites_slide_and_table_text_once(tmp_path) -> None:
    from pptx import Presentation
    from pptx.util import Inches

    path = tmp_path / "deck.pptx"
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[5])
    slide.shapes.title.text = "Old Roadmap"
    table = slide.shapes.add_table(
        1,
        1,
        Inches(1),
        Inches(2),
        Inches(3),
        Inches(1),
    ).table
    table.cell(0, 0).text = "Draft metric"
    deck.save(path)

    result = file_tools._apply_office_patch_sequence_sync(
        str(path),
        [
            {"operation": "replace_text", "old_text": "Old Roadmap", "new_text": "New Roadmap"},
            {"operation": "replace_text", "old_text": "Draft metric", "new_text": "Final metric"},
        ],
    )
    assert result["patched"] is True
    assert result["file_type"] == "pptx"
    assert result["operations_applied"] == 2
    assert result["replacements"] == 2

    out = tmp_path / "deck-edited.pptx"
    out.write_bytes(result["_persisted_bytes"])
    edited = Presentation(out)
    assert edited.slides[0].shapes.title.text == "New Roadmap"
    assert edited.slides[0].shapes[-1].table.cell(0, 0).text == "Final metric"


def test_xlsx_patch_rewrites_real_workbook_package(tmp_path) -> None:
    from openpyxl import Workbook, load_workbook

    path = tmp_path / "model.xlsx"
    wb = Workbook()
    ws = wb.active
    ws.title = "Forecast"
    ws["A1"] = "Metric"
    ws["B1"] = "Value"
    ws["A2"] = "Revenue"
    ws["B2"] = 100
    wb.save(path)
    wb.close()

    result = file_tools._spreadsheet_edit_sync(
        str(path),
        {"operation": "set_cell", "sheet": "Forecast", "cell": "B2", "value": 120},
    )
    assert result["updated"] is True
    assert result["operation"] == "set_cell"

    out = tmp_path / "model-edited.xlsx"
    out.write_bytes(result["_persisted_bytes"])
    edited = load_workbook(out)
    try:
        assert edited["Forecast"]["B2"].value == 120
    finally:
        edited.close()


def test_xlsx_patch_adds_sheet_to_existing_workbook_package(tmp_path) -> None:
    from openpyxl import Workbook, load_workbook

    path = tmp_path / "model.xlsx"
    wb = Workbook()
    wb.active.title = "Forecast"
    wb.save(path)
    wb.close()

    result = file_tools._spreadsheet_edit_sync(
        str(path),
        {"operation": "add_sheet", "sheet": "Actuals"},
    )
    assert result["updated"] is True
    assert result["operation"] == "add_sheet"
    assert result["sheet"] == "Actuals"
    assert "Actuals" in result["sheet_names"]
    assert "range" not in result

    out = tmp_path / "model-edited.xlsx"
    out.write_bytes(result["_persisted_bytes"])
    edited = load_workbook(out)
    try:
        assert edited.sheetnames == ["Forecast", "Actuals"]
    finally:
        edited.close()


def test_xlsx_patch_rejects_case_insensitive_duplicate_sheet_name(tmp_path) -> None:
    from openpyxl import Workbook

    path = tmp_path / "model.xlsx"
    wb = Workbook()
    wb.active.title = "Forecast"
    wb.save(path)
    wb.close()

    result = file_tools._spreadsheet_edit_sync(
        str(path),
        {"operation": "add_sheet", "new_sheet_name": "forecast"},
    )

    assert result == {"error": "sheet_already_exists", "sheet": "forecast"}


@pytest.mark.asyncio
@pytest.mark.parametrize("scoped", [False, True])
async def test_patch_file_adds_sheet_to_xlsx_end_to_end(tmp_path, monkeypatch, scoped) -> None:
    from openpyxl import Workbook, load_workbook

    entity_id = "entity-1"
    root = tmp_path / entity_id
    root.mkdir()
    rel_path = "sheets/model.xlsx"
    path = root / rel_path
    path.parent.mkdir()
    wb = Workbook()
    wb.active.title = "Forecast"
    wb.save(path)
    wb.close()
    if scoped:
        literal_path = root / "model.xlsx"
        literal_path.write_bytes(path.read_bytes())

        async def locate_scoped_file(**kwargs):
            assert kwargs["path"] == "model.xlsx"
            return str(path), []

        monkeypatch.setattr(file_tools, "_locate_entity_file", locate_scoped_file)

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))

    async def allow_file_access(**_kwargs):
        return None

    async def fake_guard_expected_source_sha(**_kwargs):
        return file_tools.ExpectedSourceGuardResult()

    async def fake_commit_file_projection(**kwargs):
        target = root / kwargs["rel_path"]
        target.write_bytes(kwargs["data"])
        return (
            str(target),
            SimpleNamespace(synced=True, document_id="sheet-doc-1", reason=None),
            {"size": len(kwargs["data"]), "mtime_ns": 1, "source_sha256": "new-sha"},
        )

    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(file_tools, "_guard_expected_source_sha", fake_guard_expected_source_sha)
    monkeypatch.setattr(file_tools, "_commit_file_projection", fake_commit_file_projection)

    payload = json.loads(await file_tools._patch_file(
        entity_id,
        path="model.xlsx" if scoped else rel_path,
        operations=[{"op": "sheet.add", "sheet": "Actuals"}],
        user_id="user-1",
    ))

    assert payload["updated"] is True
    assert payload["operation_results"][0]["operation"] == "add_sheet"
    assert payload["path"] == rel_path
    assert payload["document_id"] == "sheet-doc-1"
    assert payload["knowledge_synced"] is True
    edited = load_workbook(path)
    try:
        assert edited.sheetnames == ["Forecast", "Actuals"]
    finally:
        edited.close()
    if scoped:
        untouched = load_workbook(literal_path)
        try:
            assert untouched.sheetnames == ["Forecast"]
        finally:
            untouched.close()


@pytest.mark.asyncio
async def test_patch_file_batches_xlsx_sheet_and_cell_edits_end_to_end(tmp_path, monkeypatch) -> None:
    from openpyxl import Workbook, load_workbook

    entity_id = "entity-1"
    root = tmp_path / entity_id
    root.mkdir()
    rel_path = "sheets/model.xlsx"
    path = root / rel_path
    path.parent.mkdir()
    wb = Workbook()
    wb.active.title = "Forecast"
    wb.save(path)
    wb.close()

    monkeypatch.setattr(file_tools, "_get_entity_root", lambda _entity_id: str(root))

    async def allow_file_access(**_kwargs):
        return None

    async def fake_guard_expected_source_sha(**_kwargs):
        return file_tools.ExpectedSourceGuardResult()

    commits: list[bytes] = []

    async def fake_commit_file_projection(**kwargs):
        commits.append(kwargs["data"])
        target = root / kwargs["rel_path"]
        target.write_bytes(kwargs["data"])
        return (
            str(target),
            SimpleNamespace(synced=True, document_id="sheet-doc-1", reason=None),
            {"size": len(kwargs["data"]), "mtime_ns": 1, "source_sha256": "new-sha"},
        )

    monkeypatch.setattr(file_tools, "runtime_guard_file_resource_access", allow_file_access)
    monkeypatch.setattr(file_tools, "runtime_guard_file_mutation", allow_file_access)
    monkeypatch.setattr(file_tools, "_guard_expected_source_sha", fake_guard_expected_source_sha)
    monkeypatch.setattr(file_tools, "_commit_file_projection", fake_commit_file_projection)

    payload = json.loads(await file_tools._patch_file(
        entity_id,
        path=rel_path,
        operations=[
            {"op": "sheet.add", "sheet": "Actuals"},
            {"op": "cell.set", "sheet": "Actuals", "cell": "A1", "value": "Revenue"},
        ],
        user_id="user-1",
    ))

    assert payload["patched"] is True
    assert payload["updated"] is True
    assert payload["operations_applied"] == 2
    assert len(commits) == 1
    edited = load_workbook(path)
    try:
        assert edited.sheetnames == ["Forecast", "Actuals"]
        assert edited["Actuals"]["A1"].value == "Revenue"
    finally:
        edited.close()
