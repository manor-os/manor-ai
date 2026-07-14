---
sidebar_position: 2
title: Quick Start
---

# Quick Start

The shortest path from a fresh clone to a running Manor AI workspace. Expect
about 5-10 minutes after Docker images are available locally.

<div className="ma-doc-actions">
  <a className="ma-button ma-button--primary" href="#the-5-minute-path">Follow the path</a>
  <a className="ma-button ma-button--secondary" href="configuration">Review configuration</a>
</div>

By the end of this guide, you should have:

- The Manor AI web app open at `http://localhost:18080`.
- A local demo account signed in.
- The API, worker, PostgreSQL, Redis, MinIO, and sandbox services running.
- A model provider key configured so agents can answer.

## Before You Start

Install:

| Dependency | Required version | Why it matters |
| --- | --- | --- |
| Docker Compose | v2 | Boots the self-hosted service stack |
| Python | 3.11 or newer | Runs local scripts, checks, and development tools |
| Node.js | 20 or newer | Builds the web app and documentation |
| Git | Current stable | Clones and updates the repository |

> Change secrets before sharing a deployment. The default `.env.example` is for
> local evaluation; internet-accessible deployments must set strong values for
> `JWT_SECRET_KEY`, MinIO credentials, and any OAuth client secrets.

## The 5-minute Path

<div className="ma-path-table">

| Step | Command or page | What you should see |
| --- | --- | --- |
| 1 | Clone the repository | A local `manor-ai` checkout |
| 2 | Start Docker Compose | Core containers become healthy |
| 3 | Open the web app | Login page at `localhost:18080` |
| 4 | Sign in | Workspace UI loads with the demo account |
| 5 | Add a model key | Chat or agents can run a simple prompt |

</div>

### 1. Clone

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
```

### 2. Start the Stack

```bash
docker compose up --build -d
```

### 3. Open Manor AI

Open:

```text
http://localhost:18080
```

Self-hosted mode seeds a local demo account by default:

```text
demo@manor.local / manor-demo
```

### 4. Configure a Model Provider

Open Settings and add a provider key for the model path you want to use.
Manor AI is BYOK in self-hosted mode; provider credentials stay in your
deployment.

### 5. Run a Smoke Test

Open Chat or Agents and send a short prompt. If the UI loads but the model does
not answer, check provider configuration first.

## Verify Services

```bash
docker compose ps
docker compose logs api --tail=100
docker compose logs worker --tail=100
docker compose logs web --tail=100
```

Expected core services:

- PostgreSQL and Redis are healthy.
- API is reachable on its configured port.
- The web app serves the React workspace.
- Worker is running for background jobs.
- MinIO is available for object storage.
- Sandbox is available if agent code execution is enabled.

## Common Local Issues

| Symptom | What to check |
| --- | --- |
| Login page does not load | `docker compose ps`, then `docker compose logs web --tail=100` |
| API calls return 500 | `docker compose logs api --tail=100` and database health |
| Agent does not answer | Model provider key, model name, and provider network access |
| File or document errors | MinIO credentials and storage configuration |
| Sandbox tool fails | Docker socket access and `SANDBOX_SERVICE_URL` |
| Port already in use | Change the host port mapping in `docker-compose.yml` or stop the conflicting service |

## Where To Go Next

- Read [Configuration](configuration.md) before exposing Manor AI to users.
- Review [Backup and Restore](operations/backup-restore.md) before storing
  important data.
- Read [HITL Governance](concepts/hitl-governance.md) before allowing agents to
  take sensitive actions.
