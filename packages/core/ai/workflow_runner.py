"""
Workflow Runner — executes step-based workflows to completion or pause.

Step types supported:
  - agent:     Run agentic_loop with a skill/prompt and tools
  - tool:      Execute a single tool call directly
  - condition: Evaluate expression against workflow variables, branch
  - wait:      Pause execution (HITL approval, timer, external event)
  - parallel:  Run multiple sub-steps concurrently

Usage:
    from packages.core.ai.workflow_runner import WorkflowRunner
    await WorkflowRunner().run(workflow_run_id)
"""
from __future__ import annotations

import ast
import asyncio
import logging
import operator
import re
import time
from datetime import datetime, timezone
from typing import Any

from packages.core.ai.runtime import (
    ChatSurface,
    runtime_attach_and_persist_workflow_runner_result,
    runtime_execute_workflow_agent_loop,
    runtime_execute_workflow_tool_step,
    runtime_invoke_skill,
    runtime_merge_prompt_appendix,
    runtime_prepare_named_tool_surface_for_turn,
    runtime_prepare_prompt_appendix_for_turn,
    runtime_prepare_trace_envelope_for_turn,
    runtime_request_for_surface_turn,
    runtime_workflow_run_context,
)
from packages.core.database import async_session
from packages.core.models.workflow import WorkflowDefinition, WorkflowRun

logger = logging.getLogger(__name__)

# ── Constants ────────────────────────────────────────────────────────────────
_STEP_TIMEOUT_SECS = 300.0
_AGENT_MAX_ROUNDS = 20
_AGENT_TEMPERATURE = 0.7

_OPS = {
    "==": operator.eq,
    "!=": operator.ne,
    ">=": operator.ge,
    "<=": operator.le,
    ">": operator.gt,
    "<": operator.lt,
}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _render_template(template: str, variables: dict) -> str:
    """Replace ``{{var}}`` placeholders with variable values."""
    def _replacer(m: re.Match) -> str:
        key = m.group(1).strip()
        return str(variables.get(key, m.group(0)))
    return re.sub(r"\{\{(\w+)\}\}", _replacer, template)


def _resolve_value(raw: str, variables: dict) -> Any:
    """Resolve a token from a condition expression.

    Checks workflow variables first, then tries to parse as a Python literal
    (number, string, bool, None).  Falls back to the raw string.
    """
    stripped = raw.strip()

    # Dotted variable access: ``result.success``
    if "." in stripped and not stripped.startswith(("'", '"')):
        parts = stripped.split(".", 1)
        root = variables.get(parts[0])
        if isinstance(root, dict):
            return root.get(parts[1], stripped)

    # Direct variable lookup
    if stripped in variables:
        return variables[stripped]

    # ``true`` / ``false`` convenience
    if stripped.lower() == "true":
        return True
    if stripped.lower() == "false":
        return False

    # Try literal (int, float, string with quotes, None, etc.)
    try:
        return ast.literal_eval(stripped)
    except Exception:
        return stripped


# ── WorkflowRunner ───────────────────────────────────────────────────────────

class WorkflowRunner:
    """Execute a WorkflowRun through its step graph.

    Each invocation of ``run()`` processes steps until the workflow completes,
    pauses (wait step / HITL), or fails.
    """

    # ── Public entry point ───────────────────────────────────────────────

    async def run(self, workflow_run_id: str) -> None:
        """Execute a workflow run to completion or pause."""
        async with async_session() as db:
            from sqlalchemy import select
            result = await db.execute(
                select(WorkflowRun).where(WorkflowRun.id == workflow_run_id)
            )
            run = result.scalar_one_or_none()
            if not run:
                logger.warning("WorkflowRunner: run %s not found", workflow_run_id)
                return
            if run.status not in ("running", "pending"):
                logger.debug("WorkflowRunner: run %s status=%s, skipping", workflow_run_id, run.status)
                return

            wf_result = await db.execute(
                select(WorkflowDefinition).where(WorkflowDefinition.id == run.workflow_id)
            )
            workflow = wf_result.scalar_one_or_none()
            if not workflow:
                run.status = "failed"
                run.error = "Workflow definition not found"
                await db.commit()
                return

            # Mark running if pending
            if run.status == "pending":
                run.status = "running"
                run.started_at = run.started_at or _utc_now()
                await db.commit()

            try:
                await self._run_loop(workflow, run, db)
            except Exception as exc:
                logger.error("WorkflowRunner: run %s failed: %s", workflow_run_id, exc, exc_info=True)
                run.status = "failed"
                run.error = str(exc)
                run.completed_at = _utc_now()
                await db.commit()

    # ── Core loop ────────────────────────────────────────────────────────

    async def _run_loop(
        self, workflow: WorkflowDefinition, run: WorkflowRun, db,
    ) -> None:
        """Loop: find runnable steps -> execute -> advance -> repeat."""
        steps = workflow.steps or []
        step_map: dict[str, dict] = {s["id"]: s for s in steps}
        max_iterations = len(steps) * 3  # safety cap

        for _ in range(max_iterations):
            runnable = self._find_runnable_steps(workflow, run)
            if not runnable:
                # No more steps to run — check if we're done
                if self._all_steps_done(step_map, run):
                    run.status = "completed"
                    run.completed_at = _utc_now()
                else:
                    # Could be waiting (paused) or stuck
                    if run.status != "paused":
                        run.status = "failed"
                        run.error = "No runnable steps and workflow not complete"
                        run.completed_at = _utc_now()
                await db.commit()
                return

            # Parallel steps: execute concurrently
            if len(runnable) > 1:
                tasks = [
                    self._execute_step_safe(step, run, db)
                    for step in runnable
                ]
                results = await asyncio.gather(*tasks)
                for step, result in zip(runnable, results):
                    self._record_step_result(step, result, run)
                    if result.get("status") == "paused":
                        await db.commit()
                        return
                    if result.get("status") == "failed":
                        run.status = "failed"
                        run.error = result.get("error", f"Step {step['id']} failed")
                        run.completed_at = _utc_now()
                        await db.commit()
                        return
            else:
                step = runnable[0]
                result = await self._execute_step_safe(step, run, db)
                self._record_step_result(step, result, run)
                if result.get("status") == "paused":
                    await db.commit()
                    return
                if result.get("status") == "failed":
                    run.status = "failed"
                    run.error = result.get("error", f"Step {step['id']} failed")
                    run.completed_at = _utc_now()
                    await db.commit()
                    return

            await db.commit()

        # Exhausted safety cap
        run.status = "failed"
        run.error = "Exceeded maximum iteration limit"
        run.completed_at = _utc_now()
        await db.commit()

    # ── Step dispatch ────────────────────────────────────────────────────

    async def _execute_step_safe(
        self, step: dict, run: WorkflowRun, db,
    ) -> dict:
        """Execute a step with timeout and error handling."""
        step_id = step["id"]
        run.current_step_id = step_id
        start = time.monotonic()

        try:
            timeout = float(step.get("config", {}).get("timeout", _STEP_TIMEOUT_SECS))
            result = await asyncio.wait_for(
                self._execute_step(step, run, db),
                timeout=timeout,
            )
        except asyncio.TimeoutError:
            result = {"status": "failed", "error": f"Step {step_id} timed out"}
        except Exception as exc:
            logger.error("WorkflowRunner: step %s error: %s", step_id, exc, exc_info=True)
            result = {"status": "failed", "error": str(exc)}

        result["duration_ms"] = (time.monotonic() - start) * 1000
        result.setdefault("step_id", step_id)
        return result

    async def _execute_step(
        self, step: dict, run: WorkflowRun, db,
    ) -> dict:
        """Execute a single step, returning a result dict."""
        step_type = step.get("type", "tool")
        variables = dict(run.variables or {})
        entity_id = run.entity_id
        # user_id = whoever triggered this workflow run; MCP calls use
        # it to resolve personal OAuth tokens.
        user_id = run.started_by or ""
        runtime_context = runtime_workflow_run_context(run)

        if step_type == "agent":
            return await self._execute_agent_step(
                step, variables, entity_id, user_id, runtime_context, db,
            )
        elif step_type == "tool":
            return await self._execute_tool_step(
                step, variables, entity_id, user_id, runtime_context,
            )
        elif step_type == "condition":
            return await self._execute_condition_step(step, variables, run)
        elif step_type == "wait":
            return self._execute_wait_step(step, run)
        elif step_type == "parallel":
            return await self._execute_parallel_step(step, variables, entity_id, run, db)
        elif step_type == "transform":
            return self._execute_transform_step(step, variables, run)
        elif step_type == "notify":
            return {"status": "completed", "output": "Notification sent (placeholder)"}
        else:
            return {"status": "failed", "error": f"Unknown step type: {step_type}"}

    # ── Agent step ───────────────────────────────────────────────────────

    async def _execute_agent_step(
        self,
        step: dict,
        variables: dict,
        entity_id: str,
        user_id: str = "",
        runtime_context: dict[str, str | None] | None = None,
        db=None,
    ) -> dict:
        """Run agentic loop for an agent-type step.

        Config keys:
          - skill: skill ID or slug to invoke via Runtime skill boundary
          - prompt / input: text prompt (supports {{var}} templates)
          - system_prompt: override system prompt for the agentic loop
          - tools: list of tool names to make available
          - max_rounds: max agentic loop iterations (default 20)
          - temperature: LLM temperature (default 0.7)
          - model: LLM model override
        """
        config = step.get("config", {})
        runtime_context = runtime_context or {}

        # If a skill is specified, delegate through the Runtime skill boundary.
        skill_ref = config.get("skill")
        if skill_ref:
            input_text = _render_template(
                config.get("input", config.get("prompt", "")),
                variables,
            )
            configured_tools = {str(name) for name in (config.get("tools") or []) if name}
            runtime_envelope = None
            allowed_tool_names = None
            runtime_request = runtime_request_for_surface_turn(
                surface=ChatSurface.WORKFLOW_AGENT_STEP,
                entity_id=entity_id,
                user_id=user_id or None,
                workspace_id=runtime_context.get("workspace_id"),
                conversation_id=runtime_context.get("conversation_id"),
                task_id=runtime_context.get("task_id"),
                message=input_text,
                legacy_path="ai.workflow_runner.skill_step",
            )
            if configured_tools:
                runtime_surface_result = runtime_prepare_named_tool_surface_for_turn(
                    runtime_request,
                    tool_names=configured_tools,
                )
                runtime_envelope = runtime_surface_result.envelope
                allowed_tool_names = runtime_surface_result.allowed_tool_names
            else:
                runtime_envelope = runtime_prepare_trace_envelope_for_turn(runtime_request)
            async with async_session() as db:
                skill_result = await runtime_invoke_skill(
                    db,
                    skill_ref,
                    entity_id,
                    input_text,
                    user_id=user_id or None,
                    workspace_id=runtime_context.get("workspace_id"),
                    conversation_id=runtime_context.get("conversation_id"),
                    task_id=runtime_context.get("task_id"),
                    allowed_tool_names=allowed_tool_names,
                    runtime_envelope=runtime_envelope,
                )
            if skill_result.get("error"):
                return await runtime_attach_and_persist_workflow_runner_result(
                    {"status": "failed", "error": skill_result["error"]},
                    runtime_envelope,
                )
            output = skill_result.get("content", "")
            # Store output in variables if output_var specified
            output_var = config.get("output_var")
            completed = {
                "status": "completed",
                "output": output,
                "output_var": output_var,
                "usage": skill_result.get("usage"),
                "tools_used": skill_result.get("tools_used", []),
            }
            return await runtime_attach_and_persist_workflow_runner_result(
                completed,
                runtime_envelope,
            )

        # No skill — run the prompt + tools through the Runtime Harness loop adapter.
        system_prompt = _render_template(
            config.get("system_prompt", "You are a helpful assistant completing a workflow step."),
            variables,
        )
        user_message = _render_template(
            config.get("prompt", config.get("input", step.get("name", "Execute this step."))),
            variables,
        )
        max_rounds = int(config.get("max_rounds", _AGENT_MAX_ROUNDS))
        temperature = float(config.get("temperature", _AGENT_TEMPERATURE))

        # Resolve tools through Runtime prompt assembly so context blocks,
        # skill descriptors, tool filtering, and the trace envelope come from
        # one source of truth.
        tool_names = config.get("tools", [])
        runtime_request = runtime_request_for_surface_turn(
            surface=ChatSurface.WORKFLOW_AGENT_STEP,
            entity_id=entity_id,
            user_id=user_id or None,
            workspace_id=runtime_context.get("workspace_id"),
            conversation_id=runtime_context.get("conversation_id"),
            task_id=runtime_context.get("task_id"),
            message=user_message,
            legacy_path="ai.workflow_runner.agent_step",
        )
        try:
            appendix = await runtime_prepare_prompt_appendix_for_turn(
                db,
                request=runtime_request,
                active_user_message=user_message,
                configured_tool_names=tool_names,
            )
            tool_schemas = appendix.tool_schemas
            allowed_tool_names = appendix.allowed_tool_names
            runtime_envelope = appendix.envelope
            system_prompt = runtime_merge_prompt_appendix(system_prompt, appendix)
        except Exception:
            logger.debug("Workflow runtime prompt appendix failed; using tool surface fallback", exc_info=True)
            runtime_surface_result = runtime_prepare_named_tool_surface_for_turn(
                runtime_request,
                tool_names=tool_names,
            )
            tool_schemas = runtime_surface_result.tool_schemas
            allowed_tool_names = runtime_surface_result.allowed_tool_names
            runtime_envelope = runtime_surface_result.envelope

        result = await runtime_execute_workflow_agent_loop(
            runtime_envelope=runtime_envelope,
            system_prompt=system_prompt,
            user_message=user_message,
            tools=tool_schemas,
            entity_id=entity_id,
            agent_id=None,
            user_id=user_id or None,
            workspace_id=runtime_context.get("workspace_id"),
            conversation_id=runtime_context.get("conversation_id"),
            task_id=runtime_context.get("task_id"),
            active_user_message=user_message,
            allowed_tool_names=allowed_tool_names,
            max_rounds=max_rounds,
            temperature=temperature,
            model=config.get("model"),
        )

        output_var = config.get("output_var")
        completed = {
            "status": "completed",
            "output": result.content,
            "output_var": output_var,
            "usage": result.usage,
            "tools_used": result.tool_calls_made,
            "rounds": result.rounds,
        }
        return await runtime_attach_and_persist_workflow_runner_result(
            completed,
            runtime_envelope,
        )

    # ── Tool step ────────────────────────────────────────────────────────

    async def _execute_tool_step(
        self,
        step: dict,
        variables: dict,
        entity_id: str,
        user_id: str = "",
        runtime_context: dict[str, str | None] | None = None,
    ) -> dict:
        """Execute a single tool for a tool-type step.

        Config keys:
          - tool: tool name to execute
          - args: dict of arguments (supports {{var}} templates in values)
          - output_var: variable name to store the result in
        """
        config = step.get("config", {})
        runtime_context = runtime_context or {}
        tool_name = config.get("tool", "")
        if not tool_name:
            return {"status": "failed", "error": "No tool specified in step config"}

        raw_args = config.get("args", {})
        rendered_args = {
            k: _render_template(str(v), variables)
            for k, v in raw_args.items()
        }
        runtime_request = runtime_request_for_surface_turn(
            surface=ChatSurface.WORKFLOW_AGENT_STEP,
            entity_id=entity_id,
            user_id=user_id or None,
            workspace_id=runtime_context.get("workspace_id"),
            conversation_id=runtime_context.get("conversation_id"),
            task_id=runtime_context.get("task_id"),
            message=str(config.get("prompt") or config.get("input") or step.get("name") or ""),
            legacy_path="ai.workflow_runner.tool_step",
        )
        tool_step_result = await runtime_execute_workflow_tool_step(
            request=runtime_request,
            tool_name=tool_name,
            arguments=rendered_args,
            active_user_message=str(config.get("prompt") or config.get("input") or step.get("name") or ""),
        )

        output_var = config.get("output_var")
        completed = {
            "status": "completed",
            "output": tool_step_result.output,
            "output_var": output_var,
        }
        return await runtime_attach_and_persist_workflow_runner_result(
            completed,
            tool_step_result.envelope,
        )

    # ── Condition step ───────────────────────────────────────────────────

    async def _execute_condition_step(
        self, step: dict, variables: dict, run: WorkflowRun,
    ) -> dict:
        """Evaluate condition and determine branch.

        Config keys:
          - expression: e.g. ``score > 0.7``, ``status == "approved"``
        Step keys:
          - true_next: list of step IDs to follow if condition is true
          - false_next: list of step IDs to follow if condition is false
        """
        condition_met = self._evaluate_condition(step, variables)

        if condition_met:
            next_steps = step.get("true_next", step.get("next", []))
        else:
            next_steps = step.get("false_next", [])

        return {
            "status": "completed",
            "output": condition_met,
            "condition_result": condition_met,
            "next_override": next_steps,
        }

    # ── Wait step ────────────────────────────────────────────────────────

    def _execute_wait_step(self, step: dict, run: WorkflowRun) -> dict:
        """Pause execution — HITL approval, timer, or external event.

        Config keys:
          - wait_type: "approval" | "timer" | "event" (default "approval")
          - message: human-readable description of what we're waiting for
        """
        config = step.get("config", {})
        wait_type = config.get("wait_type", "approval")
        message = config.get("message", f"Waiting for {wait_type}")

        run.status = "paused"
        return {
            "status": "paused",
            "output": message,
            "wait_type": wait_type,
        }

    # ── Parallel step ────────────────────────────────────────────────────

    async def _execute_parallel_step(
        self, step: dict, variables: dict, entity_id: str,
        run: WorkflowRun, db,
    ) -> dict:
        """Run multiple sub-steps concurrently.

        Config keys:
          - steps: list of inline step dicts to execute in parallel
        """
        config = step.get("config", {})
        sub_steps = config.get("steps", [])
        if not sub_steps:
            return {"status": "completed", "output": "No sub-steps to execute"}

        tasks = []
        for sub in sub_steps:
            sub.setdefault("id", f"{step['id']}_sub_{len(tasks)}")
            tasks.append(self._execute_step_safe(sub, run, db))

        results = await asyncio.gather(*tasks)

        outputs = {}
        failed = []
        for sub, result in zip(sub_steps, results):
            sub_id = sub["id"]
            outputs[sub_id] = result.get("output")
            if result.get("status") == "failed":
                failed.append(sub_id)

        if failed:
            return {
                "status": "failed",
                "error": f"Parallel sub-steps failed: {', '.join(failed)}",
                "outputs": outputs,
            }

        return {
            "status": "completed",
            "output": outputs,
        }

    # ── Transform step ───────────────────────────────────────────────────

    def _execute_transform_step(
        self, step: dict, variables: dict, run: WorkflowRun,
    ) -> dict:
        """Update workflow variables via config.set mapping."""
        config = step.get("config", {})
        transforms = config.get("set", {})
        updated_vars = dict(run.variables or {})
        for key, value in transforms.items():
            updated_vars[key] = _render_template(str(value), variables)
        run.variables = updated_vars
        return {"status": "completed", "output": updated_vars}

    # ── Condition evaluator ──────────────────────────────────────────────

    def _evaluate_condition(self, step: dict, variables: dict) -> bool:
        """Evaluate a condition expression against workflow variables.

        Supports simple expressions:
          - ``score > 0.7``
          - ``status == "approved"``
          - ``len(items) > 0``
          - ``result.success == true``

        Uses operator-based comparison with safe value resolution.
        Does NOT use eval().
        """
        config = step.get("config", {})
        expression = str(config.get("expression", "true")).strip()

        # Handle ``len(var) OP value`` pattern
        len_match = re.match(r"len\((\w+)\)\s*(==|!=|>=|<=|>|<)\s*(.+)", expression)
        if len_match:
            var_name = len_match.group(1)
            op_str = len_match.group(2)
            right_raw = len_match.group(3)
            val = variables.get(var_name)
            left_val = len(val) if hasattr(val, "__len__") else 0
            right_val = _resolve_value(right_raw, variables)
            try:
                right_val = float(right_val)
                left_val = float(left_val)
            except (ValueError, TypeError):
                pass
            op_fn = _OPS.get(op_str, operator.eq)
            return bool(op_fn(left_val, right_val))

        # Standard ``left OP right`` pattern
        for op_str in sorted(_OPS, key=len, reverse=True):
            if op_str in expression:
                parts = expression.split(op_str, 1)
                if len(parts) == 2:
                    left_val = _resolve_value(parts[0], variables)
                    right_val = _resolve_value(parts[1], variables)

                    # Numeric comparison if both sides can be numbers
                    try:
                        left_val = float(left_val)
                        right_val = float(right_val)
                    except (ValueError, TypeError):
                        left_val = str(left_val).strip().strip("'\"")
                        right_val = str(right_val).strip().strip("'\"")

                    op_fn = _OPS[op_str]
                    return bool(op_fn(left_val, right_val))

        # Bare variable name — truthy check
        val = variables.get(expression, False)
        return bool(val)

    # ── Step resolution ──────────────────────────────────────────────────

    def _find_runnable_steps(
        self, workflow_def: WorkflowDefinition, run: WorkflowRun,
    ) -> list[dict]:
        """Find steps that are ready to execute (dependencies met).

        A step is runnable when:
        1. It has not been executed yet (no entry in step_results)
        2. All steps listed in its ``depends_on`` have completed successfully
        3. It is reachable from the current execution path

        For linear workflows (steps linked by ``next``), follows the
        current_step_id pointer.  For DAG workflows with ``depends_on``,
        returns all steps whose dependencies are satisfied.
        """
        steps = workflow_def.steps or []
        step_results = run.step_results or {}
        completed_ids = {
            sid for sid, res in step_results.items()
            if res.get("status") == "completed"
        }
        current_id = run.current_step_id

        # Check if any step has depends_on — indicates DAG mode
        has_deps = any(s.get("depends_on") for s in steps)

        if has_deps:
            # DAG mode: return all steps whose deps are met and not yet run
            runnable = []
            for step in steps:
                sid = step["id"]
                if sid in step_results:
                    continue
                deps = step.get("depends_on", [])
                if all(d in completed_ids for d in deps):
                    runnable.append(step)
            return runnable

        # Linear mode: follow current_step_id / next pointers
        if current_id:
            # If current step already has a result, follow its next pointer
            if current_id in step_results:
                current_result = step_results[current_id]
                # Condition steps may override next
                next_override = current_result.get("next_override")
                if next_override:
                    next_ids = next_override if isinstance(next_override, list) else [next_override]
                else:
                    current_step = next((s for s in steps if s["id"] == current_id), None)
                    next_ids = current_step.get("next", []) if current_step else []

                runnable = []
                for nid in next_ids:
                    if nid not in step_results:
                        step = next((s for s in steps if s["id"] == nid), None)
                        if step:
                            runnable.append(step)
                return runnable
            else:
                # Current step not yet executed
                step = next((s for s in steps if s["id"] == current_id), None)
                return [step] if step else []

        # No current_step_id — find first unexecuted step
        for step in steps:
            if step["id"] not in step_results:
                return [step]
        return []

    def _all_steps_done(self, step_map: dict[str, dict], run: WorkflowRun) -> bool:
        """Check if all reachable steps have been executed."""
        step_results = run.step_results or {}

        # If every step in the definition has a result, we're done
        if all(sid in step_results for sid in step_map):
            return True

        # For linear workflows, check if the last executed step has no next
        if step_results:
            last_executed = list(step_results.keys())[-1]
            last_result = step_results[last_executed]
            last_step = step_map.get(last_executed)
            if last_step:
                next_ids = last_result.get("next_override") or last_step.get("next", [])
                if not next_ids:
                    return True

        return False

    # ── Result recording ─────────────────────────────────────────────────

    def _record_step_result(
        self, step: dict, result: dict, run: WorkflowRun,
    ) -> None:
        """Store step result and update run variables."""
        step_id = step["id"]

        # Update step_results
        step_results = dict(run.step_results or {})
        step_results[step_id] = result
        run.step_results = step_results

        # Advance current_step_id based on result
        if result.get("status") == "completed":
            # If condition step provided next_override, use those
            next_override = result.get("next_override")
            if next_override:
                next_ids = next_override if isinstance(next_override, list) else [next_override]
                run.current_step_id = next_ids[0] if next_ids else step_id
            else:
                # Use the step's declared next
                next_ids = step.get("next", [])
                run.current_step_id = next_ids[0] if next_ids else step_id

            # Store output in variables if output_var specified
            output_var = result.get("output_var")
            if output_var and result.get("output") is not None:
                updated_vars = dict(run.variables or {})
                updated_vars[output_var] = result["output"]
                run.variables = updated_vars
        elif result.get("status") == "paused":
            run.current_step_id = step_id

    # ── Re-enqueue via Celery ────────────────────────────────────────────

    @staticmethod
    def enqueue(workflow_run_id: str, delay_seconds: float = 0) -> None:
        """Dispatch a workflow run to Celery for async execution."""
        try:
            from packages.core.tasks.ai_tasks import run_workflow
            kwargs: dict = {}
            if delay_seconds > 0:
                kwargs["countdown"] = delay_seconds
            run_workflow.apply_async(args=[workflow_run_id], **kwargs)
        except Exception as exc:
            logger.warning(
                "WorkflowRunner: failed to enqueue run %s: %s",
                workflow_run_id, exc,
            )
