---
title: Upgrades and Releases
---

# Upgrades and Releases

Treat Manor OS upgrades like application releases with database migrations.

## Recommended Flow

1. Read release notes.
2. Back up PostgreSQL and object storage.
3. Pull the new source.
4. Rebuild images.
5. Start the stack.
6. Watch API, worker, and migration logs.

```bash
git pull
docker compose up --build -d
docker compose logs api --tail=200
docker compose logs worker --tail=200
```

## Rollback

Rollback depends on whether migrations were applied. Always keep a backup from
immediately before the upgrade so you can restore database and object storage
together.

## Version Tags

Public releases are published from Git tags. The release workflow creates
GitHub release notes for tags matching `v*`.
