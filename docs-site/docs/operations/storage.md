---
title: Storage
---

# Storage

Manor AI uses PostgreSQL, Redis, MinIO, and JuiceFS together.

## PostgreSQL

PostgreSQL stores relational application data. The stack expects pgvector for
semantic search.

Back up PostgreSQL before upgrades.

## Redis

Redis is used for cache, queue/broker behavior, rate limits, and JuiceFS
metadata in the default Compose setup.

## MinIO

MinIO provides S3-compatible object storage for files, document assets, and
generated artifacts.

## JuiceFS

JuiceFS gives Manor AI an entity-scoped filesystem backed by Redis metadata and
MinIO object blocks. The `juicefs-init` service handles initial setup in the
Compose stack.

## Production Notes

- Use persistent volumes.
- Monitor disk usage.
- Back up PostgreSQL and object storage together.
- Keep filesystem and database snapshots from drifting too far apart.
