"""Missing document text must not consume approval or create a write intent."""
from __future__ import annotations

from copy import deepcopy
import json

import pytest

from packages.core.ai.runtime.approval_classifier import classify_runtime_tool
from packages.core.ai.runtime.approvals import RuntimeApprovalDecision, RuntimeApprovalMiddleware
from packages.core.ai.runtime.tool_effect_intents import runtime_tool_effect_intent_scope
from packages.core.ai.runtime.tool_execution import runtime_execute_registered_tool, runtime_prepare_tool_execution
from packages.core.contracts.generate_file_input import coerce_params, generate_file_input_error


MISSING_CONTENT = "kind=document requires complete content. Use prompt for skill/media kinds."
TITLE_INSTEAD_OF_NAME = (
    "kind=document does not use title as a filename. Use name (including "
    "the extension) and keep the complete content."
)
PDF_OFFICE_TEMPLATE = (
    "A PDF cannot be used as an Office template. Read and verify the "
    "complete PDF, then use the word_document specialist to create "
    "an edited DOCX (or the PDF specialist for PDF output), without "
    "template or operations. Do not reconstruct from a partial excerpt."
)
SOURCE_INPUT_CONFLICT = (
    "source conversion cannot be combined with "
    "content/prompt/files/options/operations/template/expected_sha256. "
    "To edit a PDF, use the PDF specialist with prompt referencing "
    "the current file path, without source, template or operations. "
    "Use source only for unchanged format conversion."
)


@pytest.mark.parametrize("key", [
    "content", "prompt", "files", "options", "operations", "template", "expected_sha256",
])
@pytest.mark.parametrize("nested", [False, True])
def test_source_conversion_rejects_editing_inputs_before_dispatch(key, nested):
    editing_input = {key: "edit instructions"}
    arguments = {"kind": "pdf", "source": {"path": "uploads/chat/source.pdf"}}
    arguments.update({"params": editing_input} if nested else editing_input)
    original = deepcopy(arguments)
    assert generate_file_input_error(arguments) == SOURCE_INPUT_CONFLICT
    assert arguments == original


@pytest.mark.parametrize("arguments", [
    {"kind": "word_document", "name": "edited.docx", "template": {"path": "uploads/chat/source.pdf"}, "operations": [{}]},
    {"kind": "document", "name": "edited.docx", "params": {"template": {"path": "source.PDF"}, "operations": [{}]}},
    {"kind": "word_document", "template": {"path": "source.pdf"}, "operations": [{}]},
])
def test_pdf_cannot_be_an_office_template(arguments):
    original = deepcopy(arguments)
    assert generate_file_input_error(arguments) == PDF_OFFICE_TEMPLATE
    assert arguments == original


@pytest.mark.parametrize("arguments", [
    {"kind": "word_document", "name": "edited.docx", "template": {"path": "source.docx"}, "operations": [{}]},
    {"kind": "document", "name": "edited.pdf", "source": {"path": "source.docx"}},
    {"kind": "document", "name": "source.pdf", "content": "complete text"},
    {"kind": "pdf", "name": "edited.pdf", "prompt": "Edit uploads/chat/source.pdf"},
    {"kind": "pdf", "name": "converted.pdf", "source": {"path": "source.docx"}, "prompt": None},
])
def test_pdf_preflight_keeps_valid_routes(arguments):
    assert generate_file_input_error(arguments) is None


@pytest.mark.parametrize("extra", [
    {"title": "report.md"},
    {"title": "report.md", "name": None},
    {"title": "report.md", "name": "  "},
    {"params": {"title": "report.md"}},
    {"params": '{"title":"report.md"}'},
])
def test_document_title_cannot_silently_replace_the_default_filename(extra):
    arguments = {"kind": "document", "content": "# Complete document", **extra}
    original = deepcopy(arguments)
    assert generate_file_input_error(arguments) == TITLE_INSTEAD_OF_NAME
    assert arguments == original


@pytest.mark.parametrize("extra", [
    {"name": "report.md", "title": "Display title"},
    {"params": {"name": "report.md"}, "title": "Display title"},
    {"params": {"output_name": "report.md"}, "title": "Display title"},
    {"params": {"filename": "report.md"}, "title": "Display title"},
    {"params": '{"filename":"report.md","title":"Display title"}'},
    {}, {"title": None}, {"title": ""}, {"title": "  "},
])
def test_document_valid_names_aliases_and_truly_unnamed_defaults_remain_valid(extra):
    arguments = {"kind": "document", "content": "# Complete document", **extra}
    original = deepcopy(arguments)
    assert generate_file_input_error(arguments) is None
    assert arguments == original


@pytest.mark.parametrize("route", [
    {"kind": "diagram"},
    {"kind": "word_document"},
    {"kind": "presentation"},
    {"kind": "spreadsheet"},
    {"kind": "document", "file_type": "diagram.json"},
    {"kind": "document", "params": {"file_type": "diagram"}},
    {"kind": "document", "files": [{"path": "index.html", "content": "Hi"}]},
    {"kind": "document", "source": {"path": "source.docx", "expected_sha256": "a" * 64}},
    {"kind": "document", "params": {"source": {"path": "source.docx", "expected_sha256": "a" * 64}}},
    {"kind": "document", "operations": [{"op": "paragraph.insert"}]},
    {"kind": "document", "params": {"operations": [{"op": "paragraph.insert"}]}},
    {"kind": "document", "template": {"path": "base.docx", "expected_sha256": "a" * 64}},
])
def test_title_validation_does_not_take_over_non_plain_document_routes(route):
    arguments = {"title": "Specialist title", **route}
    original = deepcopy(arguments)
    assert generate_file_input_error(arguments) is None
    assert arguments == original


def test_generate_file_schema_explains_the_filename_field_without_adding_an_alias():
    from packages.core.ai.tools.generate_file.schema import GENERATE_FILE_SCHEMA

    properties = GENERATE_FILE_SCHEMA["function"]["parameters"]["properties"]
    assert "filename including extension" in properties["name"]["description"]
    assert "not title" in properties["name"]["description"]
    assert "title" not in properties
    assert "PDF cannot be an Office template" in properties["template"]["description"]
    assert "For PDF edits use prompt" in properties["source"]["description"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, expected_name", [
    ({}, "generated-document.md"),
    ({"name": "report.md", "title": "Display title"}, "report.md"),
    ({"params": {"filename": "report.md"}, "title": "Display title"}, "report.md"),
])
async def test_document_handler_preserves_name_selection(monkeypatch, extra, expected_name):
    from packages.core.ai.tools.generate_file import document, tool

    captured = []

    async def generate(**kwargs):
        captured.append(kwargs["name"])
        return json.dumps({"created": True, "name": kwargs["name"]})

    monkeypatch.setattr(document, "runtime_generate_document_file", generate)
    result = await tool._generate_file_handler(kind="document", content="body", **extra)
    assert json.loads(result)["name"] == expected_name
    assert captured == [expected_name]


@pytest.mark.parametrize("extra", [
    {}, {"content": None}, {"params": {}}, {"params": '{"prompt":"Write the file"}'},
    {"params": "invalid JSON"}, {"params": []},
])
def test_document_prompt_is_not_substituted_for_complete_content(extra):
    arguments = {"kind": "document", "name": "report.md", "prompt": "Write a report", **extra}
    original = deepcopy(arguments)
    assert generate_file_input_error(arguments) == MISSING_CONTENT
    assert arguments == original


@pytest.mark.parametrize("arguments", [
    {"kind": "document", "content": "# Complete document"},
    {"kind": "document", "params": {"content": "# Nested content"}},
    {"kind": "document", "params": '{"content":"Legacy nested content"}'},
    {"kind": "document", "content": None, "params": {"content": "fallback"}},
    {"kind": "document", "files": [{"path": "index.html", "content": "Hi"}]},
    {"kind": "document", "params": {"files": [{"path": "index.html", "content": "Hi"}]}},
    {"kind": "document", "source": {"path": "original.docx", "expected_sha256": "a" * 64}},
    {"kind": "document", "params": {"source": {"path": "original.docx", "expected_sha256": "a" * 64}}},
    {"kind": "document", "operations": [{"op": "paragraph.insert"}]},
    {"kind": "document", "params": {"operations": [{"op": "paragraph.insert"}]}},
    {"kind": "document", "template": {"path": "base.docx", "expected_sha256": "a" * 64}},
    {"kind": "document", "params": {"template": {"path": "base.docx", "expected_sha256": "a" * 64}}},
    {"kind": "document", "name": "flow.diagram", "prompt": "A -> B"},
    {"kind": "document", "params": {"filename": "flow.diagram.json"}, "prompt": "A -> B"},
    {"kind": "document", "params": {"output_name": "flow.diagram.json"}, "prompt": "A -> B"},
    {"kind": "document", "params": {"name": "flow.diagram.json"}, "prompt": "A -> B"},
    {"kind": "document", "file_type": ".DIAGRAM.JSON", "prompt": "A -> B"},
    {"kind": "document", "params": {"file_type": "diagram"}, "prompt": "A -> B"},
    {"kind": "presentation", "prompt": "Create a deck"},
    {"kind": "image", "prompt": "Create a photo"},
    {"kind": "search"},
])
def test_content_preflight_preserves_all_other_document_routes(arguments):
    original = deepcopy(arguments)
    assert generate_file_input_error(arguments) is None
    assert arguments == original


def test_coerce_params_keeps_the_existing_public_alias():
    from packages.core.ai.tools.generate_file.common import coerce_params as compatibility_alias

    assert compatibility_alias is coerce_params
    assert coerce_params('["not an object"]') == {}
    original = {"content": "body"}
    assert coerce_params(original) == original
    assert coerce_params(original) is not original


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments, expected_error", [
    ({"kind": "document", "prompt": "Write a report"}, MISSING_CONTENT),
    ({"kind": "document", "title": "report.md", "content": "body"}, TITLE_INSTEAD_OF_NAME),
])
async def test_direct_handler_uses_the_same_pure_preflight(monkeypatch, arguments, expected_error):
    from packages.core.ai.tools.generate_file import tool
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    async def no_dispatch(**kwargs):
        raise AssertionError("Invalid input must not reach a document handler")

    monkeypatch.setattr(tool, "handle_document", no_dispatch)
    result = await tool._generate_file_handler(**arguments)
    assert json.loads(result) == {"error": expected_error}
    # Only the shared execution preflight owns retry authority. A handler's
    # plain JSON is not proof that earlier I/O could not have happened.
    assert not isinstance(result, RuntimePreflightValidationError)


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid, expected_error", [
    ({"kind": "document", "name": "report.md", "prompt": "Write a report", "approval_token": "not-consumed"}, MISSING_CONTENT),
    ({"kind": "word_document", "name": "edited.docx", "template": {"path": "source.pdf", "expected_sha256": "a" * 64}, "operations": [{"operation": "text.replace"}]}, PDF_OFFICE_TEMPLATE),
    ({"kind": "pdf", "name": "edited.pdf", "source": {"path": "source.pdf", "expected_sha256": "a" * 64}, "prompt": "Remove AI Rev"}, SOURCE_INPUT_CONFLICT),
])
async def test_runtime_rejects_before_approval_and_intent_then_accepts_corrected_call(monkeypatch, invalid, expected_error):
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError

    events = []

    async def guard(self, request):
        events.append("approval")
        return RuntimeApprovalDecision.allow(classify_runtime_tool(
            request.tool_name, request.arguments, entity_id=request.entity_id,
            workspace_id=request.workspace_id, task_id=request.task_id,
        ), request)

    async def intent(*args, **kwargs):
        events.append("intent")

    async def handler(**kwargs):
        events.append("handler")
        assert kwargs["content"] == "# Complete body"
        return json.dumps({"status": "completed", "fs_path": "Workspaces/test/report.md"})

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    with runtime_tool_effect_intent_scope(intent):
        rejected = await runtime_execute_registered_tool(
            tool_name="generate_file", arguments=invalid, handler_resolver=lambda _: handler,
            entity_id="entity", user_id="user", workspace_id="workspace", task_id="task",
        )
        assert isinstance(rejected, RuntimePreflightValidationError)
        assert expected_error in rejected
        assert events == []
        accepted = await runtime_execute_registered_tool(
            tool_name="generate_file", arguments={"kind": "document", "name": "report.md", "content": "# Complete body"},
            handler_resolver=lambda _: handler, entity_id="entity", user_id="user",
            workspace_id="workspace", task_id="task",
        )
    assert events == ["approval", "intent", "handler"]
    assert json.loads(accepted)["status"] == "completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"kind": "document", "prompt": "Write it"},
    {"kind": "document", "title": "report.md", "content": "body"},
])
async def test_runtime_prepare_invalid_document_does_not_issue_an_authorization_receipt(monkeypatch, arguments):
    async def no_approval(*args, **kwargs):
        raise AssertionError("Input validation must run before approval consumption")

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", no_approval)
    prepared = await runtime_prepare_tool_execution(
        tool_name="generate_file", arguments=arguments,
        entity_id="entity", user_id="user",
    )
    assert prepared.blocked
    assert prepared.authorization_receipt is None
    assert prepared.consumed_approval_request_id is None


@pytest.mark.asyncio
@pytest.mark.parametrize("post_handler_result", [
    {"error": MISSING_CONTENT},
    {"error": "write failed after I/O", "not_applied": True, "retryable": True},
])
async def test_post_handler_errors_cannot_clear_an_existing_effect_intent(monkeypatch, post_handler_result):
    from packages.core.ai.runtime.tool_results import RuntimePreflightValidationError
    from packages.core.ai.runtime.workspace_delegation_actions import _delegation_effect_intent, _delegation_receipts
    from packages.core.contracts.tool_effects import ToolEffectEvidenceFactory

    pending = []
    arguments = {"kind": "document", "name": "report.md", "content": "complete"}

    async def guard(self, request):
        return RuntimeApprovalDecision.allow(classify_runtime_tool(
            request.tool_name, request.arguments, entity_id=request.entity_id,
            workspace_id=request.workspace_id, task_id=request.task_id,
        ), request)

    async def intent(name, args, call_id, classification, *rest, **kwargs):
        ref = _delegation_effect_intent(
            name, args, entity_id="entity", workspace_id="workspace", task_id="task",
            tool_call_id=call_id, classification=classification,
        )
        assert ref is not None
        pending.append(ref)

    async def handler(**kwargs):
        return json.dumps(post_handler_result)

    monkeypatch.setattr(RuntimeApprovalMiddleware, "guard_request", guard)
    with runtime_tool_effect_intent_scope(intent):
        result = await runtime_execute_registered_tool(
            tool_name="generate_file", arguments={**arguments, "_runtime_tool_call_id_from_context": "call-1"},
            handler_resolver=lambda _: handler,
            entity_id="entity", user_id="user", workspace_id="workspace", task_id="task",
        )
    assert not isinstance(result, RuntimePreflightValidationError)
    assert len(pending) == 1
    assert pending[0]["fields"]["tool_call_id"] == "call-1"
    evidence, _, _ = _delegation_receipts([
        {"role": "assistant", "tool_calls": [{"id": "call-1", "function": {
            "name": "generate_file", "arguments": json.dumps(arguments),
        }}]},
        {"role": "tool", "tool_call_id": "call-1", "content": result},
    ], entity_id="entity", workspace_id="workspace", task_id="task", authoritative_intents=pending)
    assert ToolEffectEvidenceFactory.merge_untrusted(pending, evidence) == pending
