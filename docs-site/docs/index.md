---
sidebar_position: 1
title: Quickstart Path
description: The shortest path from a fresh clone to a running Manor AI workspace.
---

# Quickstart Path

Everything you need to verify Manor AI as a self-hosted AI workspace runtime:
clone the repository, start the local stack, sign in, add a model key, inspect
the seeded workspace, and confirm that governed actions pause for human review.

<img
  src="img/manor-ai-runtime.png"
  alt="Manor AI workspace runtime showing a running workspace dashboard"
/>

## What this is, briefly

Manor AI is a self-hosted AI workspace runtime. A workspace holds goals, tasks,
documents, knowledge, agents, tools, and human approval rules in one place so
operators can see what agents are allowed to do.

The first run should prove four things:

| Idea | What to look for |
| --- | --- |
| Workspace | Goals, tasks, knowledge, rules, and agent mappings in one operating view |
| Runtime | API, worker, PostgreSQL, Redis, MinIO, and sandbox services running together |
| Governance | Plain-language rules mapped to approval and deny action patterns |
| API surface | FastAPI endpoints that match what the web app uses |

## Before the path

Install Docker Compose v2, Git, Python 3.11 or newer, and Node.js 20 or newer.
For a local evaluation, copy the example environment file as-is. For any shared
deployment, replace the generated secrets and configure provider credentials
before inviting users.

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
```

> Change secrets before sharing a deployment. The demo account and default
> `.env.example` values are only for local evaluation.

## The 5-minute path

Allow about 5-10 minutes after Docker images are available locally.

<div className="ma-path-table">

| Step | Page or command | What you should see | Approx. |
| --- | --- | --- | --- |
| 1 | `docker compose up --build -d` | Core containers become healthy | 2-5 min |
| 2 | `http://localhost:18080` | Login page and seeded demo account | 1 min |
| 3 | Settings | Provider key saved in your deployment | 1-2 min |
| 4 | Workspace | Tasks, goals, documents, and runtime score | 1 min |
| 5 | Governance | Sensitive actions require approval before tools run | 1 min |

</div>

### 1. Start the stack

```bash
docker compose up --build -d
```

This starts the web app, API, worker, PostgreSQL with pgvector, Redis, MinIO,
and the sandbox service.

### 2. Open Manor AI

Open the local web app:

```text
http://localhost:18080
```

Self-hosted mode seeds a local demo account:

```text
demo@manor.local / manor-demo
```

### 3. Add a model key

Open Settings and add a provider key for the model path you want to use. Manor
AI is BYOK in self-hosted deployments; provider credentials stay in your
deployment.

### 4. Inspect the workspace

Open the workspace view and check the operating score, goals, tasks, documents,
and agent mappings. The workspace should look like a running system, not an
empty SDK sample.

<img
  src="img/manor-ai-goals.png"
  alt="Manor AI goal execution canvas showing goals connected to workspace tasks"
/>

### 5. Check governance

Open workspace rules. Sensitive actions, such as external messages or social
posts, can require human approval. Destructive actions can be denied before a
tool runs.

<img
  src="img/manor-ai-governance.png"
  alt="Manor AI governance rules requiring approval for external messages and blocking destructive actions"
/>

## What you'll see at the end

By the end of the path you should have:

- A browser session at `http://localhost:18080`.
- A signed-in local demo account.
- The API, worker, database, cache, object storage, and sandbox services
  running.
- A workspace with tasks, goals, and governance rules visible.
- Local API documentation available from the same deployment.

<img
  src="img/manor-ai-api-reference.png"
  alt="Manor AI OpenAPI reference with authentication endpoints"
/>

## Where to go next

- [Installation](installation.md) explains local and deployment prerequisites.
- [Configuration](configuration.md) covers secrets, model providers, storage,
  and runtime settings.
- [Agents, Skills, and Tools](concepts/agents.md) explains how agents use
  workspace context.
- [HITL Governance](concepts/hitl-governance.md) explains approval and deny
  policies before agents touch external systems.
- [API Reference](api-reference.md) maps the HTTP API used by the web app.
