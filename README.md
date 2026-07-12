# Manor AI -- The AI Operating System for Enterprise Management

> A single monorepo replacing Java Spring Boot + MySQL + Vue 2 with **Python FastAPI + PostgreSQL + React**.
> AI-native from day one: every feature is built around autonomous agents, agentic loops, and intelligent automation.

| Metric | Value |
|--------|-------|
| Total lines of code | **73,485** |
| Python files | 256 |
| TypeScript files | 78 |
| Frontend pages | 50 |
| UI components | 18 |
| Tests | Marker-layered pytest suites + frontend source checks |
| API routes | 320 |
| OpenAPI paths / schemas | 233 / 243 |
| Docker services | 8 |
| Sidebar nav items | 22 |
| i18n locales | 4 (en, zh, es, ja) |
| Features | 90+ |

---

## Quick Start

### Prerequisites

| Dependency     | Version |
|----------------|---------|
| Python         | 3.11+   |
| Node.js        | 20+     |
| PostgreSQL     | 16+ (with [pgvector](https://github.com/pgvector/pgvector)) |
| Redis          | 7+      |
| Docker Compose | v2      |

### Development Setup

```bash
# 1. Clone and install
git clone https://github.com/Manor-AI/manor-os.git && cd manor-os
cp .env.example .env          # then set local secrets; configure model keys in Settings
pip install ".[dev]"
cd apps/web && npm ci && cd ../..

# 2. Start infrastructure (PostgreSQL, Redis, MinIO)
./scripts/dev.sh infra

# 3. Initialize database (runs Alembic migrations + seed data)
./scripts/dev.sh init

# 4. Start API + frontend (separate terminals)
./scripts/dev.sh api          # FastAPI on http://localhost:8000
./scripts/dev.sh web          # Vite dev server on http://localhost:3000

# 5. (Optional) Start Celery worker for background jobs
./scripts/dev.sh worker
```

Open **http://localhost:3000** to access the application.

### Docker (Production-like)

```bash
cp .env.example .env          # local defaults run; configure model keys in Settings
docker compose up --build -d   # builds and starts the self-hosted stack
```

Open **http://localhost:18080**. OSS/self-hosted mode seeds a local demo
account by default: `demo@manor.local` / `manor-demo`.

### Running Tests

```bash
make test             # PR smoke: excludes e2e/manual/slow/network/docker/cloud
make test-regression  # Broader local regression: excludes manual/network/docker/cloud
make test-e2e         # Opt-in e2e/runtime tests
make test-manual      # Opt-in manual tests
make test-all         # Everything collected by pytest
```

---

## Architecture

```
                         +------------------+
                         |   React + Vite   |  :3000
                         |   (apps/web/)    |
                         +--------+---------+
                                  |
                                  v
                     +------------+------------+
                     |    FastAPI + Uvicorn    |  :8000
                     |    (apps/api/)          |
                     +--+------+------+-------++
                        |      |      |        |
                   +----+  +---+  +---+---+ +--+-----+
                   | PG |  |Redis| |MinIO | |Sandbox |
                   | 16 |  | 7  | |      | | :8100  |
                   +----+  +--+-+ +------++ +--------+
                              |
                        +-----+------+
                        |   Celery   |
                        |   Worker   |
                        +------------+
```

### Layer Breakdown

| Layer | Path | Description |
|-------|------|-------------|
| **Core library** | `packages/core/` | AI engine, models, services, tools, sandbox SDK |
| **API server** | `apps/api/` | FastAPI routers, middleware, dependency injection |
| **Web frontend** | `apps/web/` | React 18 SPA with TypeScript, Tailwind CSS, shadcn/ui |

---

## Features (90+)

### Security & Auth
- JWT authentication with refresh tokens
- OAuth2 login (Google)
- SAML SSO integration
- TOTP two-factor authentication (2FA) with QR code setup
- RBAC with 29 granular permissions
- API key management (per-user scoped)
- Browser session management
- Audit logging

### Tasks & Projects
- Task CRUD with categories, priorities, due dates, assignments
- Kanban board with 5 views (board, list, calendar, timeline, table)
- Task templates for repeatable workflows
- Recurring / scheduled tasks
- Task collections (project grouping)
- Bulk operations (batch create, update, delete)
- CSV import / export
- Goals and OKR tracking
- Tags and favorites

### Chat & Conversations
- SSE streaming for real-time AI responses
- Conversation management (create, rename, archive)
- Conversation sharing and export
- Human-in-the-loop (HITL) approval flow
- Tool call cards with expandable results
- Sub-agent cards (agent delegation visualization)
- Knowledge sidebar (contextual RAG results)
- Chat history browser

### AI Engine
- BYOK model routing for self-hosted deployments (native provider keys configured in Settings)
- Agentic loop with tool calling (multi-step reasoning)
- Goal runner (autonomous goal decomposition)
- Task runner (AI-driven task execution)
- 18+ tools: bash, file, document, knowledge, RAG, web, system, skill, task, code, goal, MCP, manor, search, browser, calendar, email, data tools
- Agent skills system with visibility controls
- Agent memory (per-conversation context persistence)
- Sandboxed code execution (Docker-based)

### Knowledge & Documents
- Document management with version history
- pgvector RAG pipeline (semantic search + embeddings)
- Trash and restore (soft delete)
- Text extraction (PDF, DOCX, etc.)
- Knowledge groups for organization
- Rich text document editor
- File viewer (PDF, images, code)

### Agents
- Agent CRUD with system prompt editor
- Local/custom agent templates
- Agent import and reuse within the self-hosted workspace
- Agent tool bindings (strict per-agent tool access)
- Agent dashboard (usage stats, performance)
- Agent detail view with tabs

### Workflows
- Visual flow builder (drag-and-drop)
- 6 step types (action, condition, loop, parallel, human, end)
- Conditional branching with expression engine
- Workflow execution engine with step-by-step logging

### People & Organization
- Client management (CRM-like contacts)
- Staff management with roles
- Team management (departments, roles, hierarchy)
- Client portal (external-facing pages)
- Multi-workspace support
- Custom fields on any entity
- Entity-scoped data isolation

### Integrations
- 11+ self-hosted connectors (Slack, GitHub, Jira, Linear, HubSpot, Salesforce, Zendesk, Twilio, SendGrid, Google, and more)
- Webhooks with HMAC signature verification (inbound + outbound)
- Channels (multi-channel message routing)
- OAuth/Nango configuration with user-provided provider credentials

### Analytics & Reporting
- Dashboard with key metrics and trends
- Local usage accounting
- Scheduled reports with HTML / email delivery
- Activity feed with change tracking
- Operations overview

### Platform
- Redis cache layer with TTL management
- Internationalization -- i18n with 4 locales (en, zh, es, ja)
- Toast notification system
- Presence indicators (online / offline via WebSocket)
- Comments and threaded discussions
- Tags and favorites on any entity
- Global search across all entities
- Announcements system
- Onboarding flow
- Notifications (in-app + email)
- Health checks and monitoring
- Backup and restore
- Settings management

### Infrastructure
- Docker Compose stack for postgres, redis, minio, the API, worker, web app, sandbox, browser-runner, and optional sidecars/profiles
- JuiceFS + MinIO for entity-scoped filesystem storage
- GitHub Actions CI (lint, frontend build, source smoke tests, and Python tests on push/PR; tag releases publish GitHub release notes)
- Alembic database migrations
- Makefile with dev, test, lint, build, db commands
- Celery + Redis task queue for background jobs

---

## Frontend Pages

The public self-hosted app focuses on the core workspace runtime:
Chat, Dashboard, Agents, Skills, Tasks, Workspaces, Knowledge, Documents,
Flows, Reports, Integrations, Settings, API keys, Webhooks, Search, Notifications,
Activity, Goals, Memories, Messages, and file/document viewers.

---

## API Documentation

The API exposes **320 routes** across 37 router modules.

OpenAPI spec: **233 paths**, **243 schemas**.

| Resource | URL |
|----------|-----|
| Swagger UI | http://localhost:8000/api/docs |
| ReDoc | http://localhost:8000/api/redoc |
| OpenAPI spec | Generate locally with `make openapi` |

Regenerate the spec:

```bash
make openapi
```

---

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12, FastAPI, SQLAlchemy 2.0 (async), Pydantic v2 |
| Database | PostgreSQL 16 + pgvector |
| Cache / Broker | Redis 7 |
| Frontend | React 18, TypeScript, Vite, Tailwind CSS, shadcn/ui |
| AI | OpenRouter / OpenAI SDK, pgvector RAG, agentic tool-calling loop |
| Queue | Celery + Redis |
| Auth | JWT + OAuth2 + SAML SSO + TOTP 2FA |
| Storage | MinIO (object storage) + JuiceFS (entity filesystem) |
| Sandbox | Docker-based isolated code execution |
| Infra | Docker Compose, GitHub Actions CI |
| Linting | Ruff (Python), TypeScript strict mode |

---

## Project Structure

```
manor-os/
+-- apps/
|   +-- api/                  # FastAPI application
|   |   +-- main.py           # App entry point
|   |   +-- deps.py           # Dependency injection
|   |   +-- middleware/        # CORS, auth, rate-limiting
|   |   +-- routers/          # 37 route modules (agents, chat, tasks, ...)
|   +-- web/                  # React + Vite + TypeScript frontend
|       +-- src/
|           +-- pages/        # 50 route pages (Dashboard, Chat, Agents, Tasks, ...)
|           +-- components/   # 18 shared UI components (shadcn/ui)
|           +-- stores/       # State management
|           +-- lib/          # Utilities
+-- packages/
|   +-- core/                 # Shared business logic
|   |   +-- ai/              # LLM client, agentic loop, goal/task runners
|   |   |   +-- tools/       # 18+ tool modules (bash, file, doc, web, code, mcp, ...)
|   |   +-- models/          # SQLAlchemy ORM models (30 modules)
|   |   +-- services/        # Business logic services (57 modules)
|   |   +-- tasks/           # Celery background tasks
|   |   +-- sandbox/         # Sandboxed execution SDK
|   |   +-- migrations/      # Alembic database migrations
|   |   +-- config.py        # Pydantic settings
|   |   +-- database.py      # Async engine + session factory
|   |   +-- cache.py         # Redis cache layer
|   |   +-- celery_app.py    # Celery configuration
|   |   +-- i18n.py          # Internationalization
|   |   +-- permissions.py   # RBAC permission checks
+-- tests/                   # Marker-layered pytest suites
+-- scripts/
|   +-- dev.sh               # Development helper (api|web|worker|infra|init)
|   +-- init_db.py           # Database initialization + seeding
|   +-- export_openapi.py    # OpenAPI spec generator
|   +-- generate_ts_client.sh
+-- docker/
|   +-- Dockerfile.api       # API container
|   +-- Dockerfile.web       # Nginx + static build
|   +-- Dockerfile.sandbox   # Sandboxed execution container
|   +-- nginx.conf
+-- .github/workflows/
|   +-- ci.yml               # Lint + test on push/PR
|   +-- release.yml          # GitHub release notes for version tags
+-- docker-compose.yml       # Self-hosted stack with core services and optional sidecars/profiles
+-- pyproject.toml           # Python project config (PEP 621)
+-- alembic.ini              # Alembic migration config
+-- Makefile                 # dev, test, lint, build, db commands
+-- .env.example             # Configuration template
+-- LICENSE                  # Manor Sustainable Use License 1.0
```

---

## Configuration

All configuration is via environment variables. Copy `.env.example` to `.env` and set the required values.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `LLM_MODEL` | No | `anthropic/claude-sonnet-4` | Default LLM model identifier |
| `DATABASE_URL` | Yes | (see .env.example) | PostgreSQL async connection string |
| `REDIS_URL` | Yes | `redis://redis:6379/0` | Redis connection URL |
| `JWT_SECRET_KEY` | Yes | -- | Secret for JWT token signing |
| `MINIO_ENDPOINT` | No | `minio:9000` | MinIO S3-compatible storage endpoint |
| `MINIO_ACCESS_KEY` | No | `minioadmin` | MinIO access key |
| `MINIO_SECRET_KEY` | No | `minioadmin` | MinIO secret key |
| `MANOR_FS_ENABLED` | No | `true` | Enable JuiceFS entity filesystem |
| `MANOR_FS_ROOT` | No | `/mnt/manor` | JuiceFS mount path |
| `SANDBOX_SERVICE_URL` | No | `http://sandbox:8100` | Sandbox service endpoint |
| `SHELL_SANDBOX_ENABLED` | No | `true` | Allow shell tool in agents |
| `GOOGLE_CLIENT_ID` | No | -- | Google OAuth client ID (for SSO) |
| `GOOGLE_CLIENT_SECRET` | No | -- | Google OAuth client secret |
| `SEARCH_ENGINE` | No | `serper` | Web search provider (`serper` or `tavily`) |
| `SEARCH_API_KEY` | No | -- | API key for web search tool |
| `DEPLOYMENT_MODE` | No | `oss` | Self-hosted mode. |

---

## Development

### Useful Commands

```bash
make dev-api         # Start API with hot reload
make dev-web         # Start frontend dev server
make lint            # Ruff check + TypeScript type check
make format          # Auto-format Python with Ruff
make db-migrate      # Run pending Alembic migrations
make db-init         # Initialize database with seed data
make docker-up       # Build and start the Docker compose stack
make build-docker    # Build Docker images without starting services
make clean           # Remove __pycache__, .pytest_cache, build artifacts
```

### Adding a New Feature

1. **Model** -- Add SQLAlchemy model in `packages/core/models/`
2. **Service** -- Add business logic in `packages/core/services/`
3. **Router** -- Add API endpoints in `apps/api/routers/`
4. **Tests** -- Add test file in `tests/`
5. **Migration** -- Generate with `alembic revision --autogenerate -m "description"`
6. **Frontend** -- Add page/components in `apps/web/src/`

### Code Style

- Python: enforced by [Ruff](https://docs.astral.sh/ruff/) (line length 120, target Python 3.11)
- TypeScript: strict mode, no implicit any
- Async everywhere: all database and HTTP calls use `async`/`await`

---

## Deployment Modes

| Mode | `DEPLOYMENT_MODE` | Description |
|------|-------------------|-------------|
| **Self-hosted** | `oss` | Source-available deployment for running Manor OS on your own infrastructure with user-provided model keys |

Managed Manor Cloud is a separate commercial service operated by Manor AI. This
repository is the self-hosted codebase.

---

## License

[Manor Sustainable Use License 1.0](LICENSE) -- Copyright (c) 2026 Manor AI.

The public source may be self-hosted, modified, and used internally. Reselling,
white-labeling, or offering a hosted/managed competing service requires a
separate written commercial agreement with Manor AI. The Manor AI name and
marks are governed separately by [`TRADEMARKS.md`](TRADEMARKS.md).
