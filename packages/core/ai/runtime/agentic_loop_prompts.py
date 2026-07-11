from __future__ import annotations


RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX = (
    "[Context compacted — earlier tool rounds summarized]"
)
RUNTIME_AGENTIC_LLM_COMPACTION_PREFIX = "[Earlier context — LLM summary]"


def runtime_agentic_structural_compaction_message(summary: str) -> str:
    """Build the compacted-context marker inserted into the loop history."""

    return f"{RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX}\n{summary}"


def runtime_agentic_is_structural_compaction_message(content: str) -> bool:
    """Return whether a message is the structural compaction placeholder."""

    return str(content or "").startswith(RUNTIME_AGENTIC_STRUCTURAL_COMPACTION_PREFIX)


def runtime_agentic_llm_compaction_prompt(summary_text: str) -> str:
    """Build the worker-model prompt used to summarize oversized loop context."""

    return (
        "Summarize the following conversation context in 3-5 sentences. "
        "Include key findings from tool calls, decisions made, and what "
        "the user originally asked for:\n\n"
        f"{summary_text}"
    )


def runtime_agentic_llm_compaction_replacement(summary: str) -> str:
    """Build the history message that replaces the structural summary."""

    return f"{RUNTIME_AGENTIC_LLM_COMPACTION_PREFIX}\n{summary.strip()}"


def runtime_agentic_empty_response_retry_message(*, has_tools: bool) -> str:
    """Build the retry instruction after a provider returns an empty response."""

    if not has_tools:
        retry = (
            "Retry the user's latest request now and respond with the requested "
            "content directly."
        )
    else:
        retry = (
            "Retry the user's latest request now. If it requires a file change, "
            "call the appropriate file mutation tool so the permission layer can "
            "handle approval."
        )
    return (
        "[System: The model provider returned an empty response with no tool calls. "
        f"{retry}]"
    )


def runtime_agentic_truncated_tool_call_retry_message() -> str:
    """Build the retry instruction after a tool call is truncated."""

    return (
        "[System: Your last tool call was truncated because it exceeded the output limit. "
        "Use edit_file with old_text/new_text for targeted edits instead of rewriting "
        "entire files with write_file. Break large changes into multiple smaller "
        "edit_file calls.]"
    )


def runtime_agentic_auto_next_calls_message() -> str:
    """Build the instruction that lets the loop continue recommended tool calls."""

    return (
        "[System: Continue this workflow now by executing the returned "
        "recommended_next_calls in order. Do not summarize progress until "
        "these tool calls either complete, fail, or report a real human blocker.]"
    )


def runtime_agentic_max_rounds_final_prompt() -> str:
    """Build the final no-tools prompt after max_rounds is exhausted."""

    return (
        "You've used all available tool rounds. Please provide your final "
        "response based on everything you've gathered so far."
    )
