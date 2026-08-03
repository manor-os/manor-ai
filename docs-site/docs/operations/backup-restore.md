---
title: Backup and Restore
---

# Backup and Restore

Backups should cover structured data, object storage, and configuration. Take
them together: documents, knowledge, and the entity filesystem span PostgreSQL
*and* MinIO *and* Redis-held JuiceFS metadata, and a restore is only consistent
if the pieces are from the same point in time.

## What to Back Up

| What | Where it lives | Why |
| --- | --- | --- |
| PostgreSQL | `pg_data` volume | All relational data: users, workspaces, tasks, goals, runs, messages. |
| MinIO | `minio_data` volume | Uploaded files, generated artifacts, and JuiceFS data blocks. |
| Redis DB `/1` | `redis_data` volume | JuiceFS **metadata**. Without it the JuiceFS data blocks in MinIO are unreadable. |
| `.env` | host | Secrets, provider credentials, and deployment configuration. |
| OAuth app registrations | external providers | Client ids/secrets and callback URLs needed to reconnect integrations. |

## Built-in Export API

The API ships a tenant export surface for logical backups:

```text
GET  /api/v1/backup/summary          # what an export would contain
GET  /api/v1/backup/export           # export the tenant's data
POST /api/v1/backup/export/download  # download an export archive
```

This is a data-level export (suitable for migrations and offsite copies), not
a substitute for volume-level backups of a whole deployment.

## PostgreSQL

Dump:

```bash
docker compose exec postgres pg_dump -U manor manor > manor.sql
```

Restore into a compatible PostgreSQL version:

```bash
cat manor.sql | docker compose exec -T postgres psql -U manor manor
```

For large databases prefer the custom format (`pg_dump -Fc`) with
`pg_restore`, and run dumps on a schedule (cron on the host is fine for a
single-server deployment).

## MinIO and JuiceFS

Use MinIO client tooling (`mc mirror`) or volume snapshots of `minio_data`.
Two rules:

- Keep object backups coordinated with database backups — a document row
  whose file blob is missing (or vice versa) is a broken restore.
- JuiceFS data is only usable together with its metadata. Snapshot
  `redis_data` (or at minimum Redis database `/1`) at the same time as
  `minio_data`. Restoring MinIO without the matching Redis metadata leaves
  the entity filesystem empty even though the blocks exist.

Stop or quiesce the workers before snapshotting if you need a strictly
consistent filesystem image; the FUSE mount flushes its cache on clean stop.

## Restore Practice

Do not wait for an incident to test restores. Periodically restore into a
separate environment and confirm:

1. Login works and users/workspaces are present.
2. Documents open and knowledge search returns results (checks PostgreSQL,
   MinIO, *and* JuiceFS metadata alignment).
3. An agent run completes.
4. Integrations reconnect (or are cleanly re-authorized).

Keep at least one backup from immediately before every upgrade — see
[Upgrades and Releases](upgrade-release.md).
