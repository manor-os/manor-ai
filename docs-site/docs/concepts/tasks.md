---
sidebar_position: 3
title: Tasks
---

# Tasks

Tasks are the unit of accountable work: anything an agent works on becomes a
task, and any work you assign a teammate becomes a task. A task has status,
priority (1–5), an assignee (a human, a specific agent, or the master agent
when unassigned), deadlines, comments, attachments, subtasks, and — when an
agent runs it — a full execution record.

## Creating Tasks

- **Board**: **Tasks → New Task**.
- **Chat**: ask an agent — *"Create a task for tomorrow to follow up with the
  Hayes booking"*.
- **Channels**: inbound messages (email, WhatsApp, …) create tasks in the
  workspace they route to.
- **Automations and workflows**: scheduled jobs and workflow runs create the
  tasks they work on.
- **Booking links**: a confirmed booking becomes a task.

## Statuses and the Board

Task statuses: `created`, `proposed`, `pending`, `scheduled`, `in_progress`,
`waiting_on_customer`, `on_hold`, `blocked`, `completed`, `cancelled`,
`failed` — with legal transitions enforced (an illegal move returns 409).
`GET /api/v1/tasks/constants` returns the full state machine.

The board groups statuses into five columns:

| Column | Statuses |
| --- | --- |
| To Do | created, pending, proposed |
| Scheduled | scheduled |
| In Progress | in_progress |
| Needs Attention | waiting_on_customer, on_hold, blocked, failed |
| Done | completed, cancelled |

Drag cards to change status. Board and list modes, visible columns, and
column order are per-user preferences and sync across devices. A calendar
tab and CSV import are built in. Collections (categories with icon and
color) group tasks across the board.

## Task Detail

A task page shows: properties (status, priority, assignee, deadline,
category, requester), the Markdown description, comments (Markdown +
attachments; a comment on a waiting task resumes it, and @-mentioning an
agent dispatches it), subtasks, generated output files, and the execution
timeline — the plan steps, tool calls, and status changes from agent runs.

If an agent is assigned at creation, the run starts immediately. `POST
/api/v1/tasks/{task_id}/retry` re-runs a failed task (reusing or regenerating
its plan); approval steps waiting on you are decided right from the task —
see [HITL Governance](hitl-governance.md).

## Sharing Work Outside Manor

Unauthenticated, code-scoped task endpoints let external collaborators
update a task without an account: generate a session code for a task and the
holder can view it, update status, complete it, or leave a customer
evaluation (`/api/v1/public/task/...`, codes expire after 7 days).

## SLA Policies

Define SLA policies (`/api/v1/tasks/sla-policies`) and attach them to tasks;
breaches surface on the task and in reporting.

## API Summary

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/tasks`, `POST /api/v1/tasks` | List (status/workspace/category filters) and create |
| `GET /api/v1/tasks/board` | Kanban grouping with counts |
| `GET/PUT /api/v1/tasks/board-preferences` | Per-user board preferences |
| `POST /api/v1/tasks/{id}/move` | Status transition (validated) |
| `GET/PUT/DELETE /api/v1/tasks/{id}` | Read / update / delete |
| `POST /api/v1/tasks/{id}/retry`, `/hitl-response`, `/approval` | Agent-run controls and approvals |
| `GET/POST /api/v1/tasks/{id}/logs`, `/attachments`, `GET /{id}/history` | Comments, files, audit |
| `GET/POST /api/v1/tasks/categories` | Collections |
| `GET/POST /api/v1/tasks/templates`, `POST .../{id}/instantiate` | Task templates with `{{variable}}` rendering (API-only surface) |
