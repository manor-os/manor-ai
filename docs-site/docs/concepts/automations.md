---
sidebar_position: 6
title: Automations & Scheduled Jobs
---

# Automations & Scheduled Jobs

Scheduled jobs are Manor's recurring-work engine: a natural-language prompt,
a schedule, and an executor. The platform scheduler ticks every 60 seconds,
finds due jobs, and dispatches each one to the worker fleet.

Open **Jobs** in the sidebar to see every automation in your entity, with
last run, next run, status, and per-job run history.

## What a Job Contains

| Field | Meaning |
| --- | --- |
| Schedule | `cron` (recurring by expression), `every` (fixed interval), or `at` (one-shot, optionally self-deleting after it runs). |
| Timezone | Cron expressions evaluate in the job's IANA timezone — wall-clock time, not server time. |
| Prompt | The natural-language instruction (`payload_message`) the executor receives. |
| Executor | What runs — see below. |
| Context | Optional workspace, conversation, agent, and goal links so results land in the right place. |

Cron expressions use the standard 5 fields (minute, hour, day-of-month,
month, day-of-week) with `*`, lists, ranges, and `*/N` steps; day-of-week
`0` is Sunday.

## What Can Be Scheduled

| Execution type | What happens on fire |
| --- | --- |
| Agent run (default) | Creates a task and runs an agent against the prompt. |
| Workflow | Starts a [workflow](workflows.md) run from the configured definition. |
| Skill | Runs a specific skill. |
| Goal measurement | Re-measures a goal's metrics. |
| Workspace runtime jobs | Built-ins that keep a workspace healthy: strategist review, briefing, outcome evaluation, chat insight extraction. |

When you create or edit an agent job, Manor generates a **frozen execution
skill** from your prompt in the background — the job runs against that stable
script rather than re-interpreting the prompt from scratch each time, so
repeated runs behave consistently.

## Reliability

- Every fire is recorded in an append-only run log (status, duration, token
  usage, error), joined to the task and agent execution it produced —
  click a job to inspect its runs.
- Consecutive failures are counted and surfaced; the Jobs list has an
  **attention** filter that shows failing jobs with their latest error.
- Missed-run detection looks back up to 24 hours, so a briefly stopped
  worker doesn't spam catch-up runs.

## Agents Can Schedule Too

Scheduling is available to agents as a tool: in chat you can ask an agent to
"send me a summary of open tasks every weekday at 9am" and it will create the
job itself, including file-deliverable contracts (for example, "produce a
spreadsheet") that the run must satisfy.

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/jobs` | List with `search`, `status` (`all`/`enabled`/`paused`/`attention`), `agent_id`, pagination, and summary counts |
| `POST /api/v1/jobs`, `GET/PUT/DELETE /api/v1/jobs/{job_id}` | Manage jobs |
| `POST /api/v1/jobs/{job_id}/toggle` | Enable / pause |
| `POST /api/v1/jobs/{job_id}/run_now` | Fire immediately through the normal worker path |
| `GET /api/v1/jobs/{job_id}/runs`, `.../runs/{run_id}` | Run history and full run detail (task, agent execution, tool timeline) |

Scheduling requires the Celery workers to be running — see
[Docker Compose](../docker-compose#the-two-worker-split).
