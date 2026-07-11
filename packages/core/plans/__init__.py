"""Plans layer — Planner produces a DAG, PlanExecutor runs it.

Layout:
  schema   — Plan / PlanStep pydantic models. Source of truth for
             ``execution_plans.plan_dag`` content shape.
  refs     — ``${{ steps.<key>.result.<path> }}`` resolver, used by
             the executor to wire one step's output into the next.
  service  — ExecutionPlan / ExecutionStep CRUD + plan materialisation
             (turn plan_dag into one ExecutionStep row per node).
  planner  — LLM-driven Planner (single-call Claude with strict
             pydantic validation + one retry on schema failure).
  executor — In-process PlanExecutor for Demo A v0. Future M3 worker
             layer slots in by replacing _execute_step() with a
             dispatcher hand-off; outer cycle loop stays the same.
"""
from packages.core.plans.schema import Plan, PlanStep
from packages.core.plans.refs import resolve_refs, ReferenceError
from packages.core.plans.service import (
    create_plan_from_dag,
    materialize_plan_steps,
    get_plan,
    get_step,
    list_plan_steps,
    cancel_plan,
)
from packages.core.plans.executor import PlanExecutor

__all__ = [
    "Plan",
    "PlanStep",
    "resolve_refs",
    "ReferenceError",
    "create_plan_from_dag",
    "materialize_plan_steps",
    "get_plan",
    "get_step",
    "list_plan_steps",
    "cancel_plan",
    "PlanExecutor",
]
