"""ExecutionPlanWorkflow — durable per-plan orchestrator.

Replaces the Celery-driven PlanExecutor cycle when ``TEMPORAL_ENABLED``.

The workflow walks the plan DAG, dispatches non-inline steps via the
existing Dispatcher (by marking them ``pending`` and waiting for a
signal from Dispatcher.complete_lease / fail_lease / lease_needs_human),
and handles ``sleep`` / ``human`` inline because those are workflow
constructs, not worker work.

Why this instead of the Celery cycle:

  * ``sleep`` for days survives any restart.
  * Human-in-the-loop waits don't burn a Celery slot per cycle.
  * Replay debug — every step transition is in the workflow event
    history, viewable in Temporal Web.

Constraints (Temporal workflow rules):

  * No DB / network / time / random in workflow code itself — every
    side effect is an Activity call. (See activities.py.)
  * Workflow code must be deterministic — replays must produce the
    same activity invocations in the same order.
  * Signals (the worker → workflow plumbing) are async, idempotent,
    and arrive at-least-once.
"""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

from temporalio import workflow
from temporalio.common import RetryPolicy

# All side-effecting code lives in activities — import at workflow
# definition time is allowed if marked passed-through.
with workflow.unsafe.imports_passed_through():
    from packages.core.temporal_app.activities import (
        announce_plan_started,
        finalize_plan,
        load_plan_dag,
        mark_step_done_inline,
        mark_step_pending_for_dispatcher,
        mark_step_skipped,
        mark_step_waiting_human,
        resolve_step_refs,
    )


_DEFAULT_RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    maximum_attempts=3,
)
"""Activity retries handle transient infra wobble (DB hiccup) but
don't try to paper over real bugs — bound at 3 attempts."""


_LEASE_WAIT_TIMEOUT = timedelta(hours=1)
"""How long a step waits for a worker signal before timing out. Hour
is generous — covers slow LLM steps and worker hiccups; failed-to-
signal steps roll over to next workflow tick which retries."""


_HUMAN_WAIT_TIMEOUT = timedelta(days=30)
"""Upper bound on human-input waits. 30 days is "user got hit by a
bus" territory; before that ``cancel`` signal is the escape hatch."""


@workflow.defn(name="ExecutionPlan")
class ExecutionPlanWorkflow:
    """One workflow execution per ExecutionPlan row.

    Workflow id convention: ``plan-{plan_id}`` (see client.py
    ``workflow_id_for_plan``). Idempotent on retry — re-starting a
    completed plan returns its prior result without side effect."""

    def __init__(self):
        # Signal accumulators — workflow.wait_condition watches these.
        self._step_results: dict[str, Optional[dict]] = {}
        self._step_failures: dict[str, dict] = {}
        self._human_inputs: dict[str, dict] = {}
        self._cancelled = False

    # ── Signals ──────────────────────────────────────────────────────

    @workflow.signal
    async def step_completed(
        self, step_key: str, result: Optional[dict] = None,
    ) -> None:
        """Dispatcher.complete_lease → workflow."""
        # Last write wins on duplicates (worker retries the signal).
        self._step_results[step_key] = result or {}

    @workflow.signal
    async def step_failed(self, step_key: str, error: dict) -> None:
        """Dispatcher.fail_lease (no-retry-left path) → workflow."""
        self._step_failures[step_key] = error

    @workflow.signal
    async def human_input(self, step_key: str, response: dict) -> None:
        """Operator answered a HITL prompt — workspace_chat
        resolve_pending_action signals here."""
        self._human_inputs[step_key] = response

    @workflow.signal
    async def cancel(self) -> None:
        """Operator cancelled the plan in the UI."""
        self._cancelled = True

    # ── Queries ──────────────────────────────────────────────────────

    @workflow.query
    def get_progress(self) -> dict:
        """Snapshot — visible in Temporal Web."""
        return {
            "completed_steps": list(self._step_results.keys()),
            "failed_steps": list(self._step_failures.keys()),
            "waiting_on_humans": list(self._human_inputs.keys()),
            "cancelled": self._cancelled,
        }

    # ── Main run ─────────────────────────────────────────────────────

    @workflow.run
    async def run(self, plan_id: str) -> dict:
        # 1. Load plan + transition draft → running.
        # NOTE: temporalio's execute_activity takes ``args=[…]`` for
        # multi-arg activities (single positional arg only when there's
        # exactly one). We use the keyword form everywhere for
        # consistency.
        plan_data = await workflow.execute_activity(
            load_plan_dag, args=[plan_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DEFAULT_RETRY,
        )
        await workflow.execute_activity(
            announce_plan_started, args=[plan_id],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DEFAULT_RETRY,
        )

        results: dict[str, Any] = {}
        steps = plan_data["steps"]
        keys_in_plan = {s["key"] for s in steps}

        # 2. Walk steps. Topo order is implicit in the load (we
        # materialised in topo order earlier). Within each step we
        # check deps explicitly so we can mark dependents skipped on
        # upstream failure.
        for step in steps:
            if self._cancelled:
                await workflow.execute_activity(
                    finalize_plan, args=[plan_id, "cancelled"],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_DEFAULT_RETRY,
                )
                return {"status": "cancelled", "results": results}

            step_key = step["key"]
            kind = step["kind"]
            deps = step.get("depends_on", []) or []

            # Dependency check — propagate failure / skip.
            blocked = False
            for dep in deps:
                if dep not in keys_in_plan:
                    blocked = True
                    break
                if dep in self._step_failures:
                    blocked = True
                    break
            if blocked:
                await workflow.execute_activity(
                    mark_step_skipped, args=[plan_id, step_key, "blocked_by_failed_dep"],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_DEFAULT_RETRY,
                )
                results[step_key] = {"skipped": True, "reason": "blocked_by_failed_dep"}
                continue

            # ── Inline kinds — workflow handles directly ──
            if kind == "sleep":
                seconds = float(step.get("params", {}).get("seconds") or 0)
                if seconds > 0:
                    await workflow.sleep(timedelta(seconds=seconds))
                await workflow.execute_activity(
                    mark_step_done_inline,
                    args=[plan_id, step_key, {"slept": seconds}],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_DEFAULT_RETRY,
                )
                results[step_key] = {"slept": seconds}
                continue

            if kind == "human":
                prompt = str(step.get("params", {}).get("prompt") or "")
                await workflow.execute_activity(
                    mark_step_waiting_human, args=[plan_id, step_key, prompt],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_DEFAULT_RETRY,
                )
                # Block until operator answers OR plan cancelled.
                try:
                    await workflow.wait_condition(
                        lambda: step_key in self._human_inputs or self._cancelled,
                        timeout=_HUMAN_WAIT_TIMEOUT,
                    )
                except TimeoutError:
                    self._step_failures[step_key] = {
                        "type": "HumanInputTimeout",
                        "message": f"no operator response in {_HUMAN_WAIT_TIMEOUT}",
                    }
                    continue
                if self._cancelled:
                    continue
                response = self._human_inputs[step_key]
                await workflow.execute_activity(
                    mark_step_done_inline, args=[plan_id, step_key, response],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_DEFAULT_RETRY,
                )
                results[step_key] = response
                continue

            # ── Worker-driven kinds — go through the Dispatcher ──
            # Resolve any ${{ refs }} against prior results before dispatch.
            resolved = await workflow.execute_activity(
                resolve_step_refs, args=[plan_id, step_key, results],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_DEFAULT_RETRY,
            )
            if resolved is None:
                # Reference resolution failed — step already marked failed.
                self._step_failures[step_key] = {"type": "ReferenceError"}
                continue

            await workflow.execute_activity(
                mark_step_pending_for_dispatcher, args=[plan_id, step_key],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_DEFAULT_RETRY,
            )

            # Wait for the worker (via Dispatcher signal) to report back.
            try:
                await workflow.wait_condition(
                    lambda: step_key in self._step_results
                            or step_key in self._step_failures
                            or self._cancelled,
                    timeout=_LEASE_WAIT_TIMEOUT,
                )
            except TimeoutError:
                self._step_failures[step_key] = {
                    "type": "LeaseTimeout",
                    "message": f"no worker signal in {_LEASE_WAIT_TIMEOUT}",
                }
                continue

            if self._cancelled:
                continue

            if step_key in self._step_failures:
                # Step failed terminally. Bail the whole plan — replan
                # is out of scope here (Strategist could open a new
                # plan after seeing this one's failure).
                await workflow.execute_activity(
                    finalize_plan, args=[plan_id, "failed"],
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_DEFAULT_RETRY,
                )
                return {
                    "status": "failed",
                    "results": results,
                    "failed_step": step_key,
                    "error": self._step_failures[step_key],
                }

            results[step_key] = self._step_results[step_key]

        # 3. All steps reached terminal — finalise.
        final_status = "completed" if not self._step_failures else "failed"
        await workflow.execute_activity(
            finalize_plan, args=[plan_id, final_status],
            start_to_close_timeout=timedelta(seconds=30),
            retry_policy=_DEFAULT_RETRY,
        )
        return {"status": final_status, "results": results}
