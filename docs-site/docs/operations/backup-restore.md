---
title: Backup and Restore
---

# Backup and Restore

Backups should cover structured data, object storage, and configuration.

## What to Back Up

- PostgreSQL database.
- MinIO bucket data.
- Redis data if used for durable metadata in your deployment.
- `.env` or secret manager entries.
- Any external provider configuration needed for OAuth and webhooks.

## PostgreSQL

Example:

```bash
docker compose exec postgres pg_dump -U manor manor > manor.sql
```

Restore into a compatible PostgreSQL version:

```bash
cat manor.sql | docker compose exec -T postgres psql -U manor manor
```

## MinIO

Use MinIO client tooling or volume snapshots. Keep object backups coordinated
with database backups.

## Restore Practice

Do not wait for an incident to test restores. Periodically restore into a
separate environment and confirm login, documents, knowledge search, and agent
runs still work.
