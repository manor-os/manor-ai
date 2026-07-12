---
sidebar_position: 5
title: Docker Compose
---

# Docker Compose

The Compose stack is the fastest way to run a production-like Manor OS instance
on one host.

## Core Services

| Service | Role |
| --- | --- |
| `web` | Nginx-served React frontend and API proxy. |
| `api` | FastAPI application. |
| `worker` | Celery worker for background jobs and agent tasks. |
| `postgres` | Primary relational database with pgvector support. |
| `redis` | Cache, broker, rate-limit backend, and JuiceFS metadata. |
| `minio` | S3-compatible object storage. |
| `juicefs-init` | Formats and mounts entity filesystem storage. |
| `sandbox` | Isolated code execution service. |
| `sandbox-skill-image` | Build helper image for sandbox execution. |
| `browser-runner` | Chromium and Playwright sidecar for browser automation. |
| `vault` | Local secret-encryption helper for development-style deployments. |

Optional profiles include additional services such as Temporal, Nango, and
observability tools.

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
docker compose up -d api worker
```

## Health Checks

Use `docker compose ps` first. If a service is unhealthy, inspect logs and
dependencies:

```bash
docker compose logs postgres --tail=100
docker compose logs redis --tail=100
docker compose logs api --tail=200
```

## Persistent Data

Compose volumes hold database, Redis, MinIO, and other state. Do not delete
volumes unless you intend to reset the deployment.

Create backups before upgrades. See [Backup and Restore](operations/backup-restore.md).
