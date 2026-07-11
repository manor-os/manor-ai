"""Execution layer: plans (Planner output) + steps (atomic units).

Replaces the old ``GoalRun`` / ``StepRun`` pair, which conflated three
different concepts (a persistent business goal, a Planner output, and
a per-LLM-call log). The new shape:

  Goal              persistent business north-star (models/goal.py)
  Task              human-meaningful unit of work (models/task.py)
  ExecutionPlan     a DAG of steps that Planner produced for one Task
  ExecutionStep     one atomic node in the DAG — single side effect
                    or single LLM call. Carries strict typing
                    (kind / service_key / provider) and optional
                    JSON Schema contracts on input/output so the
                    PlanExecutor can validate before/after each lease.

A Task may have many Plans over time (replan after failure → new plan
with parent_plan_id pointing at the previous one). The "current" plan
is the one in a non-terminal status; rolling history is preserved.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, TimestampMixin, generate_ulid


class ExecutionPlan(Base, TimestampMixin):
    """Planner-produced DAG of steps for a single Task."""

    __tablename__ = "execution_plans"
    __table_args__ = (
        Index("ix_plans_task", "task_id"),
        Index("ix_plans_workspace_status", "workspace_id", "status"),
        Index("ix_plans_entity_status", "entity_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))
    task_id: Mapped[Optional[str]] = mapped_column(String(26))

    agent_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))
    # Plan owner subscription. Steps may delegate to other subscriptions
    # via step.service_key, but this records "who is primarily
    # accountable" at the plan level (mirrors task.owner_subscription_id
    # at plan-creation time and is otherwise stable).

    plan_dag: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Canonical structure produced by Planner:
    # {
    #   "steps": [
    #     {"key": "...", "kind": "...", "service_key": "...",
    #      "provider": "...", "action_key": "...",
    #      "capability_id": "...", "params": {...},
    #      "depends_on": [...], "expected_output_schema": {...}, ...},
    #     ...
    #   ],
    #   "metadata": {"planner_model": "...", "estimated_cost_usd": ...}
    # }
    # On launch, the PlanExecutor materialises one execution_steps row
    # per node so the dispatcher can query by step id directly.

    planner_version: Mapped[Optional[str]] = mapped_column(String(32))
    parent_plan_id: Mapped[Optional[str]] = mapped_column(String(26))

    status: Mapped[str] = mapped_column(String(24), nullable=False, default="draft")
    # draft | pending_approval | running | paused | completed
    # | failed | cancelled | needs_attention
    approval_required: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    execution_mode: Mapped[str] = mapped_column(
        String(16), nullable=False, default="live", server_default="live"
    )
    # live      — real side effects
    # dry_run   — adapter.simulate_action(); LLM steps still execute
    # sandbox   — full sandbox workspace (dry_run + fake measurements)

    cost_tracking: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    evaluation: Mapped[Optional[dict]] = mapped_column(JSONB)
    # Post-completion self-eval: {goal_impact_estimate, lessons, ...}.

    dispatcher_state: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    # Reserved for the M3 worker/dispatcher layer (current_step_keys,
    # waiting_signals, etc). Keep null-safe access in services.

    last_error: Mapped[Optional[dict]] = mapped_column(JSONB)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class ExecutionStep(Base, TimestampMixin):
    """One atomic node in a Plan's DAG."""

    __tablename__ = "execution_steps"
    __table_args__ = (
        UniqueConstraint("plan_id", "step_key", name="uq_steps_plan_key"),
        Index("ix_steps_plan_status", "plan_id", "step_status"),
    )

    id: Mapped[str] = mapped_column(String(26), primary_key=True, default=generate_ulid)
    plan_id: Mapped[str] = mapped_column(String(26), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(26), nullable=False)
    workspace_id: Mapped[Optional[str]] = mapped_column(String(26))

    step_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Stable identifier within the plan; ``${{ steps.<key>.result }}``
    # references resolve against this on lease creation.

    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # llm              — single LLM call (no tool execution)
    # action           — call an integration adapter action_key
    # sleep            — wait until <params.until>; pure timer
    # human            — wait for explicit user input
    # subagent         — delegate to a sub-agent (multi-turn loop)
    # parallel_fanout  — spawn N parallel branches with same template
    # gather           — collect parallel branches' results
    # code             — run code in sandbox

    service_key: Mapped[Optional[str]] = mapped_column(String(100))
    # Semantic intent ('content_creator'). Resolved → subscription at
    # first dispatch by the dispatcher's matchmaker.
    resolved_subscription_id: Mapped[Optional[str]] = mapped_column(String(26))
    resolved_agent_id: Mapped[Optional[str]] = mapped_column(String(26))

    # Action specifics — null for non-action kinds
    provider: Mapped[Optional[str]] = mapped_column(String(64))
    action_key: Mapped[Optional[str]] = mapped_column(String(64))
    capability_id: Mapped[Optional[str]] = mapped_column(String(80))
    integration_id: Mapped[Optional[str]] = mapped_column(String(26))

    params: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # Input to this step. May contain ${{ steps.X.result.path }}
    # references which the dispatcher resolves at lease time.

    expected_input_schema: Mapped[Optional[dict]] = mapped_column(JSONB)
    expected_output_schema: Mapped[Optional[dict]] = mapped_column(JSONB)
    # JSON Schema; hydrated for action kinds from Runtime action binding
    # catalog schemas when known, written by Planner LLM for llm/subagent
    # kinds when downstream steps consume structured output.

    result: Mapped[Optional[dict]] = mapped_column(JSONB)
    evidence_refs: Mapped[list] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    # Object keys (MinIO / local FS) for screenshots, raw responses,
    # large blobs that don't belong in result.

    depends_on: Mapped[list] = mapped_column(JSONB, nullable=False, server_default="[]")

    step_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    # pending | running | waiting_human | done | failed | skipped | cancelled
    # Distinct from Task.status — Task uses an 11-state business machine,
    # Step uses this 7-state execution machine. They are aggregated for
    # display but never confused at the data layer.

    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    risk_level: Mapped[str] = mapped_column(
        String(8), nullable=False, default="low", server_default="low"
    )
    requires_approval: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    current_lease_id: Mapped[Optional[str]] = mapped_column(String(26))
    # Forward reference to work_leases.id (M3 worker layer). Null until
    # then; the in-process executor in the Demo A v0 sprint uses an
    # ephemeral lease so the same code paths work end-to-end.

    human_input_prompt: Mapped[Optional[str]] = mapped_column(Text)
    human_input_response: Mapped[Optional[dict]] = mapped_column(JSONB)
    # Lease-level HITL: when an action throws NeedsHumanAuth, prompt
    # is written here, then user reply lands in human_input_response,
    # and the next attempt picks it up via params.

    cost: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default="{}")
    # {llm_tokens_input, llm_tokens_output, api_calls, usd}
    error: Mapped[Optional[dict]] = mapped_column(JSONB)

    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
