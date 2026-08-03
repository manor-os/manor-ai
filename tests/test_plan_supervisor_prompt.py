"""Regression: the plan-supervisor prompt must judge the task's own deliverable.

Prod incident (task 01KXHTP7QWQJTHXHQFV9X9EQEM, 2026-07-15): a "compile status
report -> email operator" task ran to completion (both steps done, email sent),
but the plan supervisor — which runs on a cheap model (openai/gpt-4o-mini) —
returned ``needs_replan``. ``_finalize`` then marked the whole task ``failed``,
discarding the delivered email.

Root cause was the supervisor PROMPT: it only said "do not mark completed just
because every step is done", with no guidance separating "the task's deliverable
was produced" from "the subject the report is about is unresolved". On a weak
model that nudge biases toward not-completed, so a report that correctly states
"these items need your decision" was read as the *task* being unfinished.

An LLM eval (old vs new prompt, weak model, the real case + contrast cases)
confirmed the new wording flips the real case to ``completed`` while keeping a
missing/empty deliverable at ``needs_replan`` and a user-input case at
``needs_human``. That judgment can't run deterministically in CI, so this test
locks in the prompt wording the eval depends on.
"""

from packages.core.ai.runtime.planning import (
    RUNTIME_PLAN_SUPERVISOR_VERDICTS,
    runtime_plan_supervisor_prompt,
)


def _prompt() -> str:
    return runtime_plan_supervisor_prompt(
        task_title="Consolidated status report on 3 in-progress tasks -> operator email",
        task_description="Compile a consolidated status email and send it to the operator.",
        done_count=2,
        failed_count=0,
        skipped_count=0,
        steps=[
            {
                "key": "search_and_compile_report", "kind": "subagent", "status": "done",
                "instruction": "Compile the consolidated report",
                "result": "Section 1 ... needs your decision",
                "artifacts": ["Workspaces/W/reports/status.md"],
            },
            {
                "key": "send_status_email", "kind": "action", "status": "done",
                "instruction": "Send the report to the operator",
                "result": "Status: sent",
                "artifacts": [],
            },
        ],
    )


def test_prompt_directs_judging_the_deliverable_not_the_subject():
    prompt = _prompt()
    # Judge the task's own deliverable, not the reported subject.
    assert "deliverable" in prompt
    assert "not the subject it reports on" in prompt
    # Reporting carve-out: a report that surfaces blockers/decisions is a success.
    assert "SUCCESSFUL report" in prompt
    assert "decide or act on OTHER work" in prompt


def test_prompt_scopes_replan_and_human_verdicts():
    prompt = _prompt()
    # needs_replan is reserved for a missing/empty/wrong deliverable, not for
    # content that merely recommends follow-up.
    assert "Reserve needs_replan" in prompt
    assert "Reserve needs_human" in prompt
    assert "not merely because its output recommends" in prompt


def test_prompt_drops_the_misleading_anti_completion_nudge():
    prompt = _prompt()
    # The old bare nudge (with no deliverable-vs-subject guidance) is what a weak
    # model over-applied. It must not come back unqualified.
    assert "Do not mark completed only because every execution step is marked done" not in prompt


def test_prompt_lists_all_four_verdicts():
    prompt = _prompt()
    for verdict in RUNTIME_PLAN_SUPERVISOR_VERDICTS:
        assert verdict in prompt, verdict


def test_prompt_shows_instructions_results_and_artifact_receipts():
    """The supervisor judges deliverables, so it must see what each step was
    ASKED to do and what the system actually RECORDED it producing — the old
    150-character result line allowed neither."""
    prompt = _prompt()
    assert "asked to: Compile the consolidated report" in prompt
    assert "reported: Section 1" in prompt
    assert "artifacts: Workspaces/W/reports/status.md" in prompt
    # A done step with no recorded artifact says so explicitly — the signal
    # that separates "claimed to save" from "verifiably saved".
    assert "artifacts: (none recorded)" in prompt
    assert "do not take the step's own prose as proof of delivery" in prompt


def test_prompt_carries_plan_context_when_present():
    prompt = runtime_plan_supervisor_prompt(
        task_title="T",
        task_description="D",
        done_count=1, failed_count=0, skipped_count=0,
        steps=[{"key": "s1", "kind": "subagent", "status": "done", "result": "ok"}],
        plan_rationale="Only one production step is needed because the deliverable is a saved MP4.",
        is_replan=True,
    )
    assert "Planner rationale: Only one production step" in prompt
    assert "already a REPLAN" in prompt


def test_prompt_carries_the_tasks_history_and_the_no_repeat_rule():
    """The supervisor's scope is the task: earlier attempts and its own
    earlier reviews are in view, with an explicit rule against prescribing
    what was already tried and found wanting."""
    prompt = runtime_plan_supervisor_prompt(
        task_title="T", task_description="D",
        done_count=1, failed_count=0, skipped_count=0,
        steps=[{"key": "assemble", "kind": "subagent", "status": "done", "result": "same output"}],
        prior_attempts=[{
            "status": "failed",
            "steps": [{"key": "assemble", "status": "failed", "error": "TimeoutError: timed out"}],
        }],
        prior_reviews=[{
            "verdict": "retry_step", "step_key": "assemble",
            "evidence": "output was empty",
        }],
    )
    assert "You are the supervisor for one task." in prompt
    assert "Earlier attempts on this task:" in prompt
    assert "assemble(failed: TimeoutError: timed out)" in prompt
    assert "Your earlier reviews of this task:" in prompt
    assert 'retry_step (step assemble) — "output was empty"' in prompt
    assert "do not ask for the same thing again" in prompt
