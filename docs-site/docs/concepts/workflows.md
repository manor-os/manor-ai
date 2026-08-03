---
sidebar_position: 5
title: Workflows
---

# Workflows

Workflows are node-graph automations — a first-class primitive next to agents.
Where an agent decides its own steps at run time, a workflow executes a graph
you designed: deterministic where you want determinism, with `llm` and `agent`
nodes where you want intelligence.

Open **Flows** in the sidebar to use the editor. A workflow definition is
reusable and context-free; you *deploy* it into a workspace with a binding,
and every execution is recorded as a run with full step-by-step history.

## Definitions, Bindings, Runs

| Object | What it is |
| --- | --- |
| **Definition** | The reusable graph: named steps, connections, variables, and a trigger type. Definitions belong to your entity and can be shared across workspaces. |
| **Binding** | A deployment of a definition into a run context — typically a workspace plus a trigger (`manual`, `webhook`, `schedule`, `event`, or `workspace_event`). One definition can have many bindings, each with its own variables and enabled state. At run time the binding's workspace supplies connectors, knowledge, approvers, and budget. |
| **Run** | One execution. Captures a snapshot of the definition at start, per-step results, a sanitized execution trace, retry lineage, and final status. |

Run statuses move `pending → running → paused → completed / failed /
cancelled`. `paused` means the run is waiting — on an approval, a timer, or an
event — and can be resumed.

## Building a Workflow

Four ways to get a graph:

1. **Blank canvas** — add nodes from the searchable palette, connect them,
   configure each node in the side panel.
2. **Templates** — built-in starting points such as a daily digest or
   support-email triage.
3. **Import** — bring an existing export from **n8n**, **Dify**, or
   **ComfyUI**; Manor maps their node types onto its own.
4. **AI edit** — describe the automation in natural language and let the AI
   generate or modify the graph. Nothing is persisted until you save.

Every workflow must have exactly one entry node — a `trigger` or a `webhook` —
and saving is rejected without one. You can test any single node in isolation
("run node") before running the whole graph.

Values flow between steps with `{{stepId.output}}` template variables;
workflow-level variables are available as `{{varName}}`, and the trigger
payload as `{{trigger}}`.

## Node Types

| Category | Nodes |
| --- | --- |
| Entry | `trigger`, `webhook` |
| AI | `llm`, `rag` (knowledge retrieval), `agent` (full agent loop), `classifier`, `extract` |
| Actions | `tool`, `connector` (one integration call — Gmail, Notion, Slack, …), `code`, `http`, `notify`, `respond` |
| Logic | `condition` (IF), `switch`, `loop`, `parallel`, `merge`, `wait`, `stop`, `end` |
| Data | `transform`, `filter`, `aggregate`, `datetime`, `split`, `limit`, `sort`, `dedupe`, `extractfromfile` |
| Media | `image`, `video`, `audio`, `media` |
| Orchestration | `subworkflow`, `foreach_subworkflow` (map child runs over a list) |
| State & approvals | `workflow_project` (durable state shared across runs), `workflow_action_grant` (durable approval scope) |
| Annotation | `note` (canvas comment, skipped at runtime) |

An `agent` node runs the same agentic loop as chat: give it a prompt, an
optional system prompt, tool list, model, and round cap — or point it at a
workspace service so the workspace's assigned agent handles the step.

## A Minimal Example

The built-in "Daily digest" template is three working steps:

```json
[
  { "id": "t", "type": "trigger", "name": "Daily trigger", "next": ["r"] },
  { "id": "r", "type": "rag", "name": "Gather updates",
    "config": { "query": "this week's updates", "limit": 10 }, "next": ["l"] },
  { "id": "l", "type": "llm", "name": "Summarize",
    "config": { "prompt": "Summarize into a short digest:\n{{r.output}}" }, "next": ["n"] },
  { "id": "n", "type": "notify", "name": "Post digest",
    "config": { "channel": "slack", "message": "{{l.output}}" }, "next": ["e"] },
  { "id": "e", "type": "end", "name": "Done", "next": [] }
]
```

## Triggers

| Trigger | How it fires |
| --- | --- |
| `manual` | The Run button, or `POST /api/v1/workflows/{id}/run`. |
| `webhook` | `POST /api/v1/workflows/webhook/{token}` — a public endpoint where the token is the binding's shared secret. The run executes inline, so a `respond` node can return a synchronous HTTP response to the caller. |
| `schedule` | A scheduled job created from the binding, fired by the platform scheduler. |
| `event` / `workspace_event` | Platform events routed to matching enabled bindings via `POST /api/v1/workflows/trigger`. |

A binding can also register as a **chat entrypoint** for its workspace: when a
chat message clearly matches the workflow's purpose, workspace chat prefills
the workflow's inputs from the message and starts a run, and the run's
progress is projected back into the conversation as it executes.

## Approvals and Human-in-the-Loop

A `wait` node with `wait_type: "approval"` pauses the run and surfaces a
review card (optionally with a rendered payload) to the run owner. Resuming
requires an explicit decision by the run owner; anything else is rejected.
Timer waits sleep inline when short and durably re-enqueue when long; event
waits pause until resumed.

A follow-up `workflow_action_grant` node can convert an approved wait into a
durable, time-boxed grant, so repeated runs of the same workflow don't
re-prompt for an action that was already approved at that scope.

## Runs, Retries, and Observability

Every run stores its definition snapshot, per-step outputs, and a sanitized
execution trace (secrets scrubbed, large outputs summarized, artifacts and
child runs linked). The run detail view streams node status live via SSE.

- **Cancel / resume**: `POST /api/v1/workflows/runs/{run_id}/cancel` and
  `/resume`.
- **Retry**: `/retry` restarts a failed run; `from_step_id` retries from a
  checkpoint, reusing results of unchanged earlier steps. Checkpoint retries
  are refused (HTTP 409) if the definition changed since the original run.
- **Lineage**: `GET /runs/{run_id}/family` returns the retry chain.
- Results of unchanged steps can be reused between runs for cacheable node
  types, ComfyUI-style, so iterating on the tail of a graph is fast.

## API Summary

All under `/api/v1/workflows`:

| Endpoint | Purpose |
| --- | --- |
| `GET` / `POST` `""`, `GET/PUT/DELETE /{id}` | Manage definitions |
| `POST /ai-edit` | AI-generate or edit a graph (SSE stream) |
| `POST /run-node` | Execute a single node standalone |
| `POST /import` | Import an n8n / Dify / ComfyUI export |
| `GET/POST /bindings`, `PUT/DELETE /bindings/{id}` | Manage deployments |
| `POST /bindings/{id}/run`, `POST /{id}/run`, `POST /{id}/run-stream` | Start runs (inline, async, or SSE-streamed) |
| `POST /trigger`, `POST /webhook/{token}` | Event and webhook entry points |
| `GET /runs`, `GET /runs/{run_id}`, `GET /runs/{run_id}/family` | Run history |
| `POST /runs/{run_id}/cancel` / `resume` / `retry`, `POST /runs/{run_id}/step` | Run control |

## Availability

Flows are enabled by default in local and development environments. In
production environments the navigation item shows as **Soon** until the
deployment sets `FLOWS_AVAILABLE=true` — see
[Configuration](../configuration#feature-rollout).
