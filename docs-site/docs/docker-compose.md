---
sidebar_position: 5
title: Docker Compose
---

# Docker Compose

The Compose stack is the fastest way to run a production-like Manor AI instance
on one host.

## Core Services

These start on a plain `docker compose up`:

| Service | Role |
| --- | --- |
| `web` | Nginx-served React frontend. Listens on `18080` and proxies `/api` to the API container. |
| `api` | FastAPI application. |
| `worker` | Celery control-plane worker **with beat**: schedules jobs, dispatches leases, runs ops monitoring. Consumes the default `celery` queue. |
| `worker-work` | Celery work-queue worker: runs long agent plan steps on the `work` queue so long steps cannot stall scheduling. |
| `postgres` | PostgreSQL 16 with pgvector (`pgvector/pgvector:pg16`). |
| `redis` | Cache, Celery broker, rate-limit backend, and JuiceFS metadata. |
| `minio` | S3-compatible object storage. |
| `juicefs-init` | One-shot: formats and mounts entity filesystem storage, then exits. |
| `ollama` + `ollama-init` | Local embedding runtime. `ollama-init` preloads `mxbai-embed-large` on first boot so document RAG works out of the box; without the model, embedding calls fail and search degrades to text-only. |
| `sandbox` | Isolated code-execution service managing per-run Docker containers. |
| `sandbox-skill-image` | Builds the base image used by sandbox child containers, so `docker compose up --build -d` needs no separate prebuild step. |
| `vault` | Local secret-encryption helper (Vault transit) for development-style deployments. |

### The two-worker split

`worker` and `worker-work` share one image and configuration; only the queue
and concurrency differ. The control-plane worker (`-Q celery`, concurrency
`WORKER_CONTROL_CONCURRENCY`, default 2) runs beat, lease dispatch, and
monitoring. The work worker (`-Q work`, concurrency `WORKER_WORK_CONCURRENCY`,
default 4) runs user-facing plan steps and has no beat and no Docker-socket
access. They must be deployed together: beat only *enqueues* work; nothing
consumes the `work` queue until `worker-work` is up.

### Redis database allocation

One Redis instance serves four roles on separate logical databases:

| DB | Used for |
| --- | --- |
| `/0` | Application cache, rate limits, presence (`REDIS_URL`) |
| `/1` | JuiceFS metadata (`JUICEFS_META_URL`) |
| `/2` | Celery broker |
| `/3` | Celery result backend |

If you point Manor at an external Redis, keep these separated.

## Optional Profiles

Enable extra services with `--profile <name>`:

| Profile | Services | What it adds |
| --- | --- | --- |
| `nango` | `nango-server`, `nango-postgres` | Self-hosted [Nango](integrations/nango) for 200+ SaaS OAuth integrations. |
| `wechat` | `wechat-runner` | WeChat channel bridge. After start, open `http://localhost:8801/qr.png` and scan with WeChat to pair. |
| `observability` | `jaeger` | Jaeger all-in-one for OpenTelemetry traces (enable with `OTEL_ENABLED=true`). |
| `jimeng` | `jimeng-api` | Optional Jimeng image-generation gateway. |

```bash
docker compose --profile nango up -d
```

## Common Commands

```bash
docker compose up --build -d
docker compose ps
docker compose logs api --tail=100
docker compose logs worker --tail=100
docker compose down
```

## Rebuilding One Service

```bash
docker compose build api
docker compose up -d api worker worker-work
```

The `api`, `worker`, and `worker-work` services share the `manor-api` image —
rebuild once, restart all three.

## Health Checks

Use `docker compose ps` first; every long-running service defines a
healthcheck, and dependent services wait for `service_healthy`. If a service
is unhealthy, inspect logs and its dependencies:

```bash
docker compose logs postgres --tail=100
docker compose logs redis --tail=100
docker compose logs api --tail=200
```

The API also exposes `GET /health`, `GET /health/ready`, and
`GET /health/deep` (checks database, Redis, and storage). The control-plane
worker probes `http://api:8000/health/deep` from inside the Compose network as
part of ops monitoring.

Startup order is enforced through dependencies: `juicefs-init` and
`ollama-init` must complete before the API and workers start, so a slow first
boot (image pulls, model download) is normal.

## Persistent Data

Named volumes hold all state:

| Volume | Contents |
| --- | --- |
| `pg_data` | PostgreSQL data directory |
| `redis_data` | Redis persistence |
| `minio_data` | Object storage (uploads, JuiceFS data blocks) |
| `ollama_data` | Downloaded embedding models |
| `wechat_runner_data` | WeChat session state (profile `wechat`) |
| `nango_pg_data` | Nango's own database (profile `nango`) |

Do not delete volumes unless you intend to reset the deployment. Create
backups before upgrades — see [Backup and Restore](operations/backup-restore.md).

## Stopping Cleanly

The workers mount JuiceFS over FUSE and get a 30-second grace period on stop
to drain in-flight tasks and flush cache to MinIO. Prefer
`docker compose stop` / `docker compose down` over killing containers; a
SIGKILL mid-flush can leave a wedged FUSE mount that requires manual cleanup.
