---
sidebar_position: 4
title: Goals & Plans
---

# Goals & Plans

Goals are measurable business outcomes; plans are how agent work gets
decomposed, executed, and audited. Together they form the chain from "what we
want" to "what actually ran":

```text
Goal  ←→  Task  →  ExecutionPlan  →  ExecutionStep
```

A goal links to the tasks that advance it; a task that an agent works gets an
execution plan; a plan is a DAG of typed steps the workers execute.

## Goals

A goal lives in a workspace (or at the entity level) and carries:

- A **metric**: `metric_key` (for example `follower_count`, `mrr`), baseline,
  current, and target values, and an optional deadline.
- **Pace**: `on_track`, `behind`, `ahead`, `at_risk`, `achieved`, or
  `unknown` — computed from measurements against the target.
- **Status**: `active`, `achieved`, `abandoned`, `paused`. When pace reaches
  the target, the goal flips to `achieved` automatically and its measurement
  schedule stops.
- **Measurement**: an optional measurement source and cadence; measurements
  run as [scheduled jobs](automations.md) and append to a time series you
  can chart.

Goals appear in the workspace's **Goals** tab as cards with pace badges and
progress, plus a pan-and-zoom goal graph that renders the full
Goal → Task → Plan-step chain. Tasks are linked to goals with a contribution
type (`direct`, `indirect`, `discovered`) and estimated impact, so the
workspace can show *why* each piece of work exists.

Agents can create and link goals themselves — asking "track a goal of 1,000
newsletter subscribers by June" in workspace chat is enough.

## Plans and Steps

When an agent takes on a task, the planner produces an **execution plan**: a
DAG of steps with dependencies, where each step has a kind (`llm`, `action`,
`subagent`, `code`, `human`, `sleep`, `parallel_fanout`, `gather`), a
service key resolved to a concrete agent at dispatch time, a risk level, and
cost tracking. Step outputs feed later steps by reference.

Plan statuses run `draft → running → completed`, with `pending_approval`
(plan needs human sign-off before executing), `paused`, `needs_attention`,
`failed`, `cancelled`, and `replanned` (the executor produced a successor
plan; lineage is kept). Step-level `waiting_human` is where
[HITL approvals](hitl-governance.md) pause execution.

Everything is inspectable from the task detail's execution timeline, or via
the API.

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET/POST /api/v1/goals`, `GET/PUT/DELETE /api/v1/goals/{id}` | Manage goals |
| `GET/POST /api/v1/goals/{id}/measurements` | Measurement time series |
| `GET /api/v1/plans` | List plans (by workspace, task, status) |
| `GET /api/v1/plans/{id}`, `GET /api/v1/plans/{id}/steps` | Inspect a plan |
| `POST /api/v1/plans/from-task/{task_id}` | Invoke the planner for a task |
| `POST /api/v1/plans/{id}/approve` / `/cancel` | Human sign-off / stop |
| `POST /api/v1/plans/{id}/retry-failed-steps`, `POST /api/v1/plans/steps/{step_id}/retry` | Recovery; retrying a waiting-human step records the approval |

Goal templates (`GET /api/v1/goal-templates`) offer ready-made recipes —
daily briefing, email triage, X/Twitter growth, and more — that mint a goal
plus its seed tasks and measurement schedule in one call
(`POST /api/v1/workspaces/{workspace_id}/apply-template`).
