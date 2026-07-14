# Manor AI

[![Docs](https://img.shields.io/badge/docs-manor--ai-blue)](https://manor-os.github.io/docs/manor-ai/)
[![License](https://img.shields.io/badge/license-Sustainable%20Use-lightgrey)](LICENSE)
[![CI](https://img.shields.io/badge/ci-GitHub%20Actions-24292f)](.github/workflows/ci.yml)

Manor AI is a self-hosted AI workspace runtime for teams that want agents,
documents, tasks, workflows, tools, and integrations under their own control.
It is built for BYOK deployments, local data ownership, and auditable
human-in-the-loop automation.

![Manor AI workspace runtime screenshot](https://raw.githubusercontent.com/manor-os/manor-ai/main/docs-site/static/img/manor-ai-runtime.png)

**Start here:** [Documentation](https://manor-os.github.io/docs/manor-ai/) ·
[Quick Start](https://manor-os.github.io/docs/manor-ai/quickstart) ·
[Roadmap](ROADMAP.md) · [Contributing](CONTRIBUTING.md) ·
[Security](SECURITY.md)

## What Manor AI Is For

- Run an AI workspace on your own infrastructure with user-provided model keys.
- Give agents scoped tools, workspace context, documents, and approval gates.
- Build operational workflows where people can inspect, approve, and audit
  important automation steps.
- Connect self-hosted integrations through webhooks, OAuth providers, Nango,
  and local services you control.

## Quick Start

### Prerequisites

| Dependency | Version |
| --- | --- |
| Docker Compose | v2 |
| Python | 3.11+ |
| Node.js | 20+ |
| Git | Current stable |

### Self-host Manor AI with Docker Compose

```bash
git clone https://github.com/manor-os/manor-ai.git && cd manor-ai
cp .env.example .env
docker compose up --build -d
```

Open **http://localhost:18080**.

Self-hosted mode seeds a local demo account by default:

```text
demo@manor.local / manor-demo
```

After signing in, add your model provider keys in Settings. Manor AI is BYOK in
self-hosted deployments; provider credentials stay in your deployment.

## What Ships In The Self-Hosted Stack

| Area | Included |
| --- | --- |
| Workspace app | React + Vite web UI for chat, tasks, agents, knowledge, documents, workflows, reports, and settings |
| API runtime | FastAPI service with auth, RBAC, audit logging, OpenAPI docs, and workspace APIs |
| Agent runtime | Tool-calling loop, skills, scoped tools, HITL approvals, task runners, and goal workflows |
| Data services | PostgreSQL 16 with pgvector, Redis, MinIO, and optional JuiceFS-backed entity storage |
| Automation | Sandboxed code execution, browser automation sidecars where configured, scheduled jobs, and Celery workers |
| Integrations | Webhooks, OAuth provider configuration, Nango support, API keys, and connector surfaces |

## Architecture

```text
Browser
  |
  v
React + Vite web app
  |
  v
FastAPI API server  ---- Celery worker
  |                     |
  +-- PostgreSQL        +-- Redis
  +-- MinIO/JuiceFS
  +-- Sandbox service
  +-- Integration sidecars
```

| Layer | Path | Description |
| --- | --- | --- |
| Core library | `packages/core/` | AI engine, models, services, tools, sandbox SDK |
| API server | `apps/api/` | FastAPI routers, middleware, dependency injection |
| Web frontend | `apps/web/` | React 18 SPA with TypeScript, Tailwind CSS, shadcn/ui |
| Docs site | `docs-site/` | Docusaurus documentation published to GitHub Pages |

## Development Setup

```bash
cp .env.example .env
pip install ".[dev]"
cd apps/web && npm ci && cd ../..

# Start infrastructure (PostgreSQL, Redis, MinIO)
./scripts/dev.sh infra

# Initialize database (Alembic migrations + seed data)
./scripts/dev.sh init

# Start API + frontend in separate terminals
./scripts/dev.sh api
./scripts/dev.sh web

# Optional Celery worker for background jobs
./scripts/dev.sh worker
```

Open **http://localhost:3000** for the development web app.

## Checks

```bash
make test             # PR smoke: excludes e2e/manual/slow/network/docker/cloud
make test-regression  # Broader local regression
make test-e2e         # Opt-in e2e/runtime tests
make test-manual      # Opt-in manual tests
make test-all         # Everything collected by pytest
```

For frontend work:

```bash
npm --prefix apps/web run build
```

For docs:

```bash
cd docs-site
npm ci
npm run build
```

## API Surface

Manor AI exposes the same FastAPI HTTP API used by the web app. Start with the
[API Reference](https://manor-os.github.io/docs/manor-ai/api-reference) for a
resource map, authentication notes, and curl examples.

| Area | Example endpoints |
| --- | --- |
| Auth and profile | `POST /api/v1/auth/login`, `GET /api/v1/auth/me` |
| Workspaces | `GET /api/v1/workspaces`, `POST /api/v1/workspaces` |
| Chat and agents | `POST /api/v1/chat/message`, `POST /api/v1/chat/stream`, `GET /api/v1/agents` |
| Tasks, goals, documents | `/api/v1/tasks`, `/api/v1/goals`, `/api/v1/documents` |
| Integrations and webhooks | `/api/v1/integrations/*`, `/api/v1/webhooks` |
| Operations | `/health`, `/health/ready`, `/api/v1/backup/*`, `/api/v1/usage/*` |

Local API docs:

| Resource | Docker Compose URL |
| --- | --- |
| Swagger UI | http://localhost:18080/api/docs |
| ReDoc | http://localhost:18080/api/redoc |
| OpenAPI JSON | http://localhost:18080/api/openapi.json |

Generate a schema for client generation with `make openapi`.

## Repository Map

```text
manor-ai/
+-- apps/
|   +-- api/                  # FastAPI application
|   +-- web/                  # React + Vite frontend
+-- packages/
|   +-- core/                 # Shared models, services, AI runtime, tools
+-- docs-site/                # Public documentation site
+-- tests/                    # Marker-layered pytest suites
+-- scripts/                  # Development and release helpers
+-- docker/                   # Container images and nginx config
+-- docker-compose.yml        # Self-hosted stack
+-- .env.example              # Configuration template
+-- LICENSE                   # Manor Sustainable Use License 1.0
```

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env`
and set values for your deployment.

| Variable | Required | Default | Description |
| --- | --- | --- | --- |
| `DEPLOYMENT_MODE` | No | `oss` | Self-hosted mode |
| `DATABASE_URL` | Yes | See `.env.example` | PostgreSQL async connection string |
| `REDIS_URL` | Yes | `redis://redis:6379/0` | Redis connection URL |
| `JWT_SECRET_KEY` | Yes | Generated locally | Secret for JWT token signing |
| `MINIO_ENDPOINT` | No | `minio:9000` | MinIO S3-compatible storage endpoint |
| `SANDBOX_SERVICE_URL` | No | `http://sandbox:8100` | Sandbox service endpoint |
| `SHELL_SANDBOX_ENABLED` | No | `true` | Allow shell tool in agents |
| `SEARCH_ENGINE` | No | `serper` | Web search provider |
| `SEARCH_API_KEY` | No | Empty | API key for web search tools |

Review [the configuration guide](https://manor-os.github.io/docs/manor-ai/configuration)
before exposing a deployment to users.

## Community

- Use GitHub issues for bugs and feature requests.
- Use GitHub Discussions for design questions, operator notes, and roadmap
  feedback once Discussions is enabled on the public repository.
- Read [CONTRIBUTING.md](CONTRIBUTING.md) before opening a pull request.
- Report suspected vulnerabilities privately through [SECURITY.md](SECURITY.md).

## License

[Manor Sustainable Use License 1.0](LICENSE) -- Copyright (c) 2026 Manor AI.

The public source may be self-hosted, modified, and used internally. Reselling,
white-labeling, or offering a hosted/managed competing service requires a
separate written commercial agreement with Manor AI. The Manor AI name and
marks are governed separately by [TRADEMARKS.md](TRADEMARKS.md).
