"""Guard against the `consecutive_truncations` circuit breaker firing on large
write_file tool calls: (1) the tool-usage prompt steers the agent to edit_file /
chunked writes for large files, and (2) the tool-call max_tokens ceiling is high
enough that whole-file content rarely truncates mid-call.
"""

from __future__ import annotations


def test_tool_usage_guidance_steers_large_files_to_edit_file():
    from packages.core.ai.runtime.prompt_guidance import runtime_tool_usage_guidance

    out = runtime_tool_usage_guidance(
        tool_names=["write_file", "edit_file", "bash"],
        has_tools=True,
    )
    assert out is not None
    low = out.lower()
    assert "edit_file" in low
    assert "large file" in low
    assert "truncat" in low  # explains WHY (oversized write_file truncates)


def test_tool_call_max_tokens_ceiling_raised():
    # Agentic runtime explicitly raises the cap so big write_file args fit,
    # while the low-level chat default remains safer for user-facing calls.
    from packages.core.ai.runtime import RUNTIME_AGENTIC_TOOL_CALL_MAX_TOKENS

    assert RUNTIME_AGENTIC_TOOL_CALL_MAX_TOKENS >= 16384
