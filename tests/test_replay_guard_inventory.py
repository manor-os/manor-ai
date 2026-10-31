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
        # A step failing before any effectful call is proven effect-free only
        # by the pre-I/O watcher seeing no call during that attempt.
        ("effect_watcher.effect_intents == intents_before", 1),
    ],
    # Transient step re-execution: never past a replay-forbidding attempt, and
    # for effectful node types only with a host-proven "none".
    "packages/core/services/workflow_step_resilience.py": [
        ('if attempt_forbids_replay(result) or result.get("retry_blocked") is True:', 1),
        ('return result.get("effect_state") == "none"', 1),
        # An interrupted attempt is effect-free only by its own bound journal.
        ('and entry.get("effect_intent") is False', 1),
    ],
    "packages/core/services/workflow_claim_recovery.py": [
        ("and not interrupted_step_proved_effect_free(run, step_id)", 1),
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
    # A durable delegated child run (W4a): a repeated wake crash fails the
    # child with an unknown last round (effect floor, interrupted), never a
    # silent fourth replay; a new delegation to an interrupted agent is
    # refused until continue_interrupted.
    "packages/core/workers/task_agent_child_loop.py": [
        ("if failures < _MAX_WAKE_FAILURES:", 1),
        ("code=CHILD_REPAIR_REQUIRED,", 1),
        ("interrupted = child_effects_unknown(checkpoint, ledger_complete=ledger_complete)", 1),
        ("ledger_complete = ledger_complete and not lost_work", 1),
        ("took_over = child.phase == TaskCoordinatorPhase.RUNNING.value and child.claim_token is not None", 1),
    ],
    "packages/core/services/task_agent_child_settlement.py": [
        # Host-ended children whose effects are unknown are interrupted
        # (do_not_replay): an incomplete or unconfirmed ledger ...
        ('{"do_not_replay": True, "effect_state": EffectState.UNCONFIRMED.value}', 1),
        ("if not ledger_complete or took_over:", 1),
        ('and failures.get("wake_generation") == wake_generation', 1),
        ('return ledger_is_unconfirmed(checkpoint.get("tool_evidence"))', 1),
        # ... and an incomplete ledger raises the parent's effect floor.
        ("if not complete:", 1),
    ],
    "packages/core/services/task_agent_delegation.py": [
        ('if latest.interrupted and item["params"].get("continue_interrupted") is not True:', 1),
    ],
    # A Task retry of a root that waited on children: the parked call keeps
    # its settled (or cancelled) result and the children's ledgers carry over.
    "packages/core/services/task_coordinator_wakeup.py": [
        ("stored = runtime_stored_pending_result(checkpoint)", 1),
        ('ledger.merge_child_observations(stored["id"], stored["child_evidence"], observe=False)', 1),
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
