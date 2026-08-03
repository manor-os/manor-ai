---
sidebar_position: 6
title: Architecture
---

# Architecture

Manor AI is a monorepo with a Python backend, React frontend, worker runtime,
and isolated execution services.

```text
React web app
    |
    v
FastAPI API  <----> PostgreSQL + pgvector
    |                 Redis
    |                 MinIO + JuiceFS
    |                 Ollama (embeddings)
    v
Celery workers (control plane + work queue)
    |
    +---- Sandbox service (per-run containers)
    +---- Integrations, channels, and webhooks
```

## Backend

The API lives in `apps/api`. It exposes workspace, chat, agent, task, goal,
workflow, document, knowledge, integration, channel, scheduling, and
operations endpoints.

Shared domain logic lives in `packages/core`:

- SQLAlchemy models
- services
- agent runtime (the agentic loop, tools, prompt context)
- workflow engine (node-graph runner, importers)
- skills
- permissions
- migrations
- Celery tasks

## Frontend

The web app lives in `apps/web`. It is a React 18 SPA built with Vite and
TypeScript. It communicates with the API through the web proxy in Docker and
directly with the API during local development.

## Worker Runtime

Background execution is split across two Celery workers that share one image:

- **Control plane** (`worker`): runs Celery beat, fires
  [scheduled jobs](concepts/automations.md), dispatches work leases, and
  monitors the deployment. Queue `celery`.
- **Work queue** (`worker-work`): executes long agent plan steps and workflow
  runs. Queue `work`.

The split keeps a handful of long-running agent steps from stalling
scheduling. Both workers mount the entity filesystem and share the API's
codebase and configuration model.

## Isolation Boundaries

- The [sandbox service](operations/sandbox.md) executes code in per-run
  containers with memory/CPU/PID limits — never in the API or worker
  processes.
- Entity filesystem paths are scoped through the Manor file service on
  JuiceFS.
- Tool access is constrained by agent settings and
  [HITL governance](concepts/hitl-governance.md).
- Everything is entity-scoped (single-tenant per deployment in OSS mode),
  with workspace-level permissions inside the entity.

## Data Stores

| Store | Role |
| --- | --- |
| PostgreSQL (+pgvector) | Source of truth for structured data and chunk embeddings |
| Redis | Cache, Celery broker/results, rate limits, JuiceFS metadata |
| MinIO | Object storage: uploads, artifacts, JuiceFS data blocks |
| JuiceFS | POSIX entity filesystem assembled from Redis metadata + MinIO blocks |
| Ollama | Local embedding model (`mxbai-embed-large`) for document RAG |

See [Storage](operations/storage.md) for operational detail.
