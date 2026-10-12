import pytest

from packages.core.services.assistant_blocks import AssistantBlocksBuilder
from packages.core.ai.runtime.streams import runtime_tool_arguments_for_chat


def arguments(**overrides):
    return {"kind": "document", "name": "converted.pdf", "file_type": "pdf", "source": {"path": "docs/input.docx", "expected_sha256": "a" * 64}, **overrides}


def finish(builder, args, *, status="success", result=None, name="generate_file"):
    args = runtime_tool_arguments_for_chat(name, args)
    builder.start_tool(name, args)
    builder.end_tool(name, arguments=args, status=status, result=result)


def test_successful_conversion_retry_preserves_error_history_but_completes_process():
    builder = AssistantBlocksBuilder()
    finish(builder, arguments(kind="word_document"), status="error", result={"error": "source conversion requires kind=document (or pdf)"})
    finish(builder, arguments())
    builder.set_final_text("Converted.")
    process = builder.blocks()[0]
    assert process["status"] == "completed"
    assert process["steps"][0]["status"] == "error"
    assert process["steps"][0]["recovered_by_step_id"] == process["steps"][1]["id"]


@pytest.mark.parametrize("change", [
    {"name": "another.pdf"}, {"file_type": "docx"}, {"storage_scope": "workspace"},
    {"source": {"path": "docs/other.docx", "expected_sha256": "a" * 64}},
    {"source": {"path": "docs/input.docx", "expected_sha256": "b" * 64}},
])
def test_different_conversion_does_not_clear_failure(change):
    builder = AssistantBlocksBuilder()
    finish(builder, arguments(), status="error")
    finish(builder, arguments(**change))
    builder.set_final_text("Some work finished.")
    assert builder.blocks()[0]["status"] == "error"


def test_correcting_scope_that_has_no_destination_recovers_only_its_conversion():
    builder = AssistantBlocksBuilder()
    finish(builder, arguments(storage_scope="workspace"), status="error", result='{"error":"storage_scope=workspace requires an active Workspace context"}')
    finish(builder, arguments())
    assert builder.blocks()[0]["status"] == "completed"


def test_final_prose_or_unrelated_success_is_not_recovery_evidence():
    builder = AssistantBlocksBuilder()
    finish(builder, arguments(), status="error")
    finish(builder, {"path": "docs/input.docx"}, name="read_file")
    builder.set_final_text("Done.")
    assert builder.blocks()[0]["status"] == "error"


def test_parallel_same_tool_results_match_their_actual_arguments():
    builder = AssistantBlocksBuilder()
    first, second = arguments(), arguments(name="second.pdf")
    builder.start_tool("generate_file", first)
    builder.start_tool("generate_file", second)
    builder.end_tool("generate_file", arguments=first, status="error")
    steps = builder.blocks()[0]["steps"]
    assert [step["status"] for step in steps] == ["error", "running"]
    builder.end_tool("generate_file", arguments=second, status="success")
    assert builder.blocks()[0]["status"] == "error"
