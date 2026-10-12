"""A sandbox skill handoff must leave the turn enough rounds to finish the workflow.

Seen end-to-end: a chat turn planned for 8 tool rounds allocated a sandbox,
read the contract, and ran out of rounds before writing the script.
"""

from __future__ import annotations

from pathlib import Path

from packages.core.ai import agentic_loop


def test_handoff_guarantees_a_minimum_remaining_budget():
    extra = agentic_loop.SANDBOX_HANDOFF_MIN_REMAINING_ROUNDS
    assert extra >= 8
    # An 8-round chat turn handing off at round 2 gets 2 + extra.
    assert agentic_loop._rounds_after_sandbox_handoff(8, 2) == 2 + extra
    # A generous budget is never shrunk, and nothing exceeds the loop default.
    assert agentic_loop._rounds_after_sandbox_handoff(200, 2) == 200
    assert agentic_loop._rounds_after_sandbox_handoff(8, 195) == agentic_loop.DEFAULT_MAX_ROUNDS
    # A caller that asked for more than the default keeps every round.
    assert agentic_loop._rounds_after_sandbox_handoff(500, 2) == 500


def test_extension_is_latched_once_per_skill_per_turn():
    """One message asking for an xlsx and then a docx hands off twice; the
    second workflow needs its own rounds (the docx save was cut off at 13
    rounds when the latch was per turn). Re-handing off the same skill must
    not extend again."""
    source = Path("packages/core/ai/agentic_loop.py").read_text(encoding="utf-8")
    assert source.count("sandbox_budget_extended_for.add(handoff_skill)") == 2
    assert source.count("handoff_skill not in sandbox_budget_extended_for") == 2
    # The latch is bound before the resume/live branch split, so a resumed
    # turn (checkpoint present) reads a bound name.
    init = source.index("sandbox_budget_extended_for: set[str] = set()")
    assert init < source.index("if checkpoint is not None:")

    assert agentic_loop._sandbox_handoff_skill_name({"status": "sandbox_ready", "skill": "DOCX"}) == "docx"
    assert agentic_loop._sandbox_handoff_skill_name({"status": "sandbox_ready"}) == "<unnamed>"


def test_generate_file_handoff_is_recognised_like_invoke_skill():
    assert {"invoke_skill", "generate_file"} <= set(agentic_loop._SANDBOX_HANDOFF_TOOL_NAMES)
    handoff = {"status": "sandbox_ready", "sandbox_id": "sb_1", "loaded_tools": ["sandbox"]}
    assert agentic_loop._invoke_skill_schema_load_names(handoff) == ["sandbox"]


def test_loop_extends_the_budget_on_both_handoff_paths():
    """The live tool path and the durable resume path (where a reserved
    sandbox's skill call is re-executed) must both extend the budget."""
    source = Path("packages/core/ai/agentic_loop.py").read_text(encoding="utf-8")
    assert source.count("extended_rounds = _rounds_after_sandbox_handoff(") == 2
    assert source.count("hard_max_rounds=hard_max_rounds,") == 2
    assert source.count("max_rounds = extended_rounds") == 2
    live = source.index('if tc["name"] in _SANDBOX_HANDOFF_TOOL_NAMES')
    resume = source.index("in _SANDBOX_HANDOFF_TOOL_NAMES\n")
    assert resume < live, "the resume block precedes the live tool loop"
