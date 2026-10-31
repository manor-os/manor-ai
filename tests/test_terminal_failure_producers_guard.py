"""``terminal_failure`` / ``stop_parent`` abort the whole model turn. Every
producer must implement "no usable alternative remains" semantics and be listed
here after review; a new literal anywhere else fails this test.

2026-09-11: search_tools aborted a turn because one semantically matched
provider (chrome) was unpaired, even though builtin web_search matched too.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "packages" / "core"
PATTERN = re.compile(r"[\"']terminal_failure[\"']\s*:\s*True")

REVIEWED_PRODUCERS = {
    # per-call MCP preflight for one specific tool: nothing else can satisfy that call
    "ai/runtime/tool_availability.py": 1,
    # skill run contract failures, failed Stickman generation/publishing, or
    # unknown action outcomes: the skill is the only path or replay is unsafe
    "ai/runtime/skills.py": 4,
    # A nested Skill must keep an uncertain effect terminal after a crash.
    "ai/runtime/nested_agent.py": 1,
    # Chrome local-browser paths (pairing, session, sequence, relay): the local
    # browser is the only path for these calls, so there is no alternative to offer
    "ai/runtime/chrome_sequence.py": 2,
    "ai/runtime/chrome_run.py": 1,
}


def test_terminal_failure_literals_only_in_reviewed_producers():
    found: dict[str, int] = {}
    for path in ROOT.rglob("*.py"):
        if "tests" in path.relative_to(ROOT).parts:
            continue  # in-package tests assert on the payload; they are not producers
        text = path.read_text(encoding="utf-8", errors="ignore")
        count = len(PATTERN.findall(text))
        if count:
            found[str(path.relative_to(ROOT))] = count
    assert found == REVIEWED_PRODUCERS, (
        "terminal_failure producers changed. Review the new/changed site for "
        "'no usable alternative' semantics, then update REVIEWED_PRODUCERS: "
        f"{found}"
    )
