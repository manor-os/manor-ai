"""Terminal-tool stop reasons are success terminators, declared once.

The agentic loop stops on purpose when a terminal tool policy matches; callers
that gate on ``stop_reason == "completed"`` must not read those deliberate
stops as failures.
"""
from __future__ import annotations


def test_terminal_success_stop_reasons_are_registry_driven():
    """The success terminator is declared once and shared, not re-spelled at
    every call site."""
    from packages.core.ai.terminal_stops import is_terminal_success_stop_reason

    assert is_terminal_success_stop_reason("skill_terminal")
    assert is_terminal_success_stop_reason("media_generation_tool_result")
    assert is_terminal_success_stop_reason("local_coding_dispatched")
    assert not is_terminal_success_stop_reason("error")
    assert not is_terminal_success_stop_reason("max_rounds")
