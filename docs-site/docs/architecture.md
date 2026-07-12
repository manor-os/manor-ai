---
sidebar_position: 6
title: Architecture
---

# Architecture

Manor OS is a monorepo with a Python backend, React frontend, worker runtime,
and isolated execution services.

```text
React web app
    |
    v
FastAPI API  <----> PostgreSQL + pgvector
    |                 Redis
    |                 MinIO + JuiceFS
    v
Celery worker
    |
    +---- Sandbox service
    +---- Browser runner
    +---- Integrations and webhooks
```

## Backend

The API lives in `apps/api`. It exposes workspace, chat, agent, document,
knowledge, integration, and operations endpoints.

Shared domain logic lives in `packages/core`:

- SQLAlchemy models
- services
- agent runtime
- tools
- skills
- permissions
- migrations
- tasks

## Frontend

The web app lives in `apps/web`. It is a React 18 SPA built with Vite and
TypeScript. It communicates with the API through the web proxy in Docker and
directly with the API during local development.

## Worker Runtime

The worker handles asynchronous jobs, agent execution, media generation,
workflow steps, and integration callbacks. It shares the same codebase and
configuration model as the API.

## Isolation Boundaries

- The sandbox service handles code execution in a constrained container.
- Browser automation runs in `browser-runner`, isolated from API and worker
  processes.
- Entity filesystem paths are scoped through the Manor file service.
- Tool access is constrained by agent settings and HITL governance.

## Data Stores

PostgreSQL is the source of truth for structured data. Redis provides queue,
cache, rate-limit, and JuiceFS metadata support. MinIO stores objects and
document assets.
