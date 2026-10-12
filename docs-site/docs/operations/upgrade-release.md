---
title: Upgrades and Releases
---

# Upgrades and Releases

Treat Manor AI upgrades like application releases with database migrations:
back up first, apply, verify, and keep a rollback path.

## Recommended Flow

1. Read the release notes / `CHANGELOG.md` for the range you are jumping.
2. Back up PostgreSQL, MinIO, and Redis `/1` together
   ([Backup and Restore](backup-restore.md)).
3. Pull the new source.
4. Rebuild images and restart the stack — migrations apply on API startup.
5. Watch API and worker logs until healthy.

Before upgrading a shared deployment, rehearse the exact installed revision to
candidate revision in an isolated database restored from your backup. Check
conversation transcripts, Workspace/task data, credential references and
in-progress run state. Experimental task engines stay off by default and
upgrading does not switch the engine of an existing run.

```bash
git pull
docker compose up --build -d
docker compose logs api --tail=200
docker compose logs worker --tail=200
```

6. Verify: sign in, open a workspace, run a quick agent chat, and check
   `GET /health/deep` returns healthy.

The `api`, `worker`, and `worker-work` services share one image; Compose
rebuilds it once and recreates all three. Both workers must come back up —
scheduling lives in `worker`, long plan steps in `worker-work`.

## Rollback

Rollback difficulty depends on whether migrations were applied:

- **No schema change**: check out the previous version and
  `docker compose up --build -d`.
- **Schema changed**: restore the pre-upgrade database (and object storage)
  backup, then start the previous version. Do not run an older application
  against a newer schema.

Always keep the backup from immediately before the upgrade so database and
object storage can be restored together.

Keep the previous image/source revision and matching configuration. Stop
execution before restoring PostgreSQL, MinIO and JuiceFS metadata as one set;
test that restore in isolation. An untested Alembic downgrade is not a rollback
guarantee. Blueprint JSON contains portable configuration, not a full backup.

## Version Tags

Public releases are published from Git tags. The release workflow creates
GitHub release notes for tags matching `v*`. Tracking `main` between tags is
possible but treat it as a rolling release: read recent commit messages for
migration-bearing changes before upgrading.
