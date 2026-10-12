"""Inventory of automatic retry paths and the replay guard each one applies.

Guard the class: adding an automatic retry/continuation path means adding it
here with the check it performs. Each entry is verified against the source so
a refactor that drops the guard fails this test. A needle that guards several
sites lists how many must remain.
"""

from __future__ import annotations

import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]

GUARDED: dict[str, list[tuple[str, int]]] = {
    "packages/core/ai/workflow_runner.py": [
        # retry_on_fail stops on an unconfirmed attempt.
        ("attempt_forbids_replay(result)", 1),
        # Imported-node auto-skip never hides an unconfirmed effect.
        ('and result.get("do_not_replay") is not True', 1),
        # Agent-step failures carry the replay fields (control + ledger):
        # prompt path, then the skill terminal / fail_on_stop_reasons / error paths.
        ("**replay_fields_from_result(result),", 1),
        ("failure.update(replay_fields_from_result(skill_result))", 2),
        ("**replay_fields_from_result(skill_result)}", 1),
    ],
    "packages/core/workers/task_coordinator.py": [
        ("not replay_forbidden", 1),
    ],
    "packages/core/services/agent_delegation_session.py": [
        ('run_result.get("do_not_replay") is True', 1),
    ],
    # The delegated child's replay fields reach its session and the parent:
    # the shared child result puts them in the parent's payload, and the chat
    # host writes the same fields to the child's session.
    "packages/core/ai/runtime/workspace_delegation_actions.py": [
        ("else replay_fields_from_result(result)", 1),
        ("**replay_fields,", 1),
        ("**child.replay_fields,", 1),
    ],
    # Both vocabularies: retryable=False (budget) and do_not_replay (effect).
    "packages/core/dispatcher/service.py": [
        ('failure.get("retryable") is False or attempt_forbids_replay(failure)', 1),
        # The definition plus every attempt/settlement call site.
        ("_has_ambiguous_external_effect(", 5),
    ],
    "packages/core/plans/executor.py": [
        ('failure.get("retryable") is False', 1),
        ("attempt_forbids_replay(failure)", 1),
        # Every branch-repair / reconciliation call site.
        ("has_ambiguous_external_effect(", 4),
    ],
}


def test_every_automatic_retry_path_keeps_its_guard():
    missing = [
        f"{path}: {needle} (expected at least {count})"
        for path, needles in GUARDED.items()
        for needle, count in needles
        if (ROOT / path).read_text().count(needle) < count
    ]
    assert missing == []
