"""A completed assistant response has one authoritative final text block."""

from copy import deepcopy

from packages.core.services.assistant_blocks import AssistantBlocksBuilder


def _final_blocks(builder):
    return [block for block in builder.blocks()
            if block.get("type") == "text" and block.get("phase") == "final"]


def test_repeated_finalization_replaces_an_earlier_summary_and_streamed_answer():
    builder = AssistantBlocksBuilder()
    builder.append_text("I will inspect the render.")
    builder.start_tool("probe_media")
    builder.end_tool("probe_media", result={"status": "running"}, status="success")
    builder.start_final_summary()
    builder.append_text("The render has started. I will inspect the finished file.", phase="final")
    builder.start_tool("probe_media")
    builder.end_tool("probe_media", result={"status": "pending"}, status="success")
    canonical = "The render is still running, so the completed file is not available yet."
    builder.append_text("\n" + canonical, phase="final")
    # The stream flush and the persistence boundary both finalize the same
    # response. The old implementation appended the canonical answer twice.
    builder.set_final_text(canonical)
    builder.set_final_text(canonical)
    finals = _final_blocks(builder)
    assert [block["text"] for block in finals] == [canonical]
    assert [block["text"] for block in builder.blocks() if block.get("phase") == "opening"] == [
        "I will inspect the render.",
    ]
    process = next(block for block in builder.blocks() if block["type"] == "process")
    assert len(process["steps"]) == 2
    assert process["default_collapsed"] is True


def test_authoritative_final_replaces_a_streamed_draft_instead_of_appending():
    builder = AssistantBlocksBuilder()
    builder.start_final_summary()
    builder.append_text("Draft answer with a detail that was corrected.", phase="final")
    builder.set_final_text("The corrected final answer.")
    after_first = deepcopy(builder.blocks())
    builder.set_final_text("The corrected final answer.")
    assert builder.blocks() == after_first
    assert [block["text"] for block in _final_blocks(builder)] == ["The corrected final answer."]


def test_replacing_final_text_preserves_process_progress_and_visualization():
    builder = AssistantBlocksBuilder()
    builder.start_tool("probe_media")
    builder.end_tool("probe_media", result={"status": "running"}, status="success")
    builder.append_text("The file is not ready.", phase="progress")
    builder.start_final_summary()
    builder.append_text("An obsolete summary.", phase="final")
    visualization = {"id": "visual", "type": "visualization", "kind": "test", "data": {"count": 1}}
    builder.blocks().append(visualization)
    builder.append_text("The file is not ready.", phase="progress", after_step_seq=2)
    retained = [deepcopy(block) for block in builder.blocks() if block.get("phase") != "final"]
    builder.set_final_text("The file is not ready.")
    assert [block for block in builder.blocks() if block.get("phase") != "final"] == retained
    assert [block["text"] for block in _final_blocks(builder)] == ["The file is not ready."]
    assert builder.blocks()[-1]["phase"] == "final"
