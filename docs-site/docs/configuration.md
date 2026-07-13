---
sidebar_position: 4
title: Configuration
---

# Configuration

Configuration is read from environment variables. Start from `.env.example`:

```bash
cp .env.example .env
```

## Required for Real Deployments

Change these before exposing Manor OS beyond local evaluation:

| Variable | Purpose |
| --- | --- |
| `JWT_SECRET_KEY` | Signs user sessions. Use a long random value. |
| `DATABASE_URL` | Async PostgreSQL connection string. |
| `DATABASE_URL_SYNC` | Sync PostgreSQL URL used by Alembic. |
| `REDIS_URL` | Redis cache and broker URL. |
| `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` | Object storage credentials. |
| `PUBLIC_BASE_URL` | Public URL used by webhooks and generated media callbacks. |
| `APP_URL` | Browser-facing web URL. |

## Model Keys

Self-hosted Manor OS is BYOK. Configure model provider credentials in Settings
for each workspace or user. Avoid baking model API keys into images or source
control.

## Storage

Manor OS uses MinIO for object storage and JuiceFS for entity-scoped filesystem
storage. The default Compose stack formats and mounts the local JuiceFS volume
automatically.

Important variables:

| Variable | Default | Notes |
| --- | --- | --- |
| `MANOR_FS_ENABLED` | `true` | Enables entity filesystem support. |
| `MANOR_FS_ROOT` | `/mnt/manor` | Mount path used by API and workers. |
| `JUICEFS_META_URL` | `redis://redis:6379/1` | JuiceFS metadata backend. |
| `JUICEFS_BUCKET` | `http://minio:9000/manor` | Object store bucket URL. |

## Rate Limits and Degraded Mode

Rate limiting is opt-in for local evaluation. For shared deployments, enable
Redis-backed limits after measuring expected traffic.

`DEGRADED_MODE=true` lets operators temporarily disable expensive routes while
keeping health, login, and basic reads available.

## OAuth and Webhooks

External providers need a stable public HTTPS URL. Set `PUBLIC_BASE_URL` to the
URL providers can reach.

For local webhook testing, use a trusted tunnel and update `PUBLIC_BASE_URL`
while the tunnel is active.
