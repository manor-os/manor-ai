---
title: Storage
---

# Storage

Manor AI uses PostgreSQL, Redis, MinIO, and JuiceFS together. Each holds a
different kind of state; understanding the split is what makes backups,
scaling, and debugging tractable.

## PostgreSQL

PostgreSQL (16, with **pgvector**) stores all relational application data:
users, entities, workspaces, tasks, goals, documents metadata, conversations,
workflow definitions and runs, and the `document_chunks` table that holds
chunk embeddings for semantic search.

- The stack ships `pgvector/pgvector:pg16`; a vanilla PostgreSQL without the
  pgvector extension will not work.
- Back up before every upgrade — see
  [Backup and Restore](backup-restore.md).
- Watch connection counts if you raise `API_WORKERS` or pool sizes; see the
  sizing note in [Configuration](../configuration#database).

## Redis

One Redis instance serves four separate roles on separate logical databases:

| DB | Role |
| --- | --- |
| `/0` | Application cache, rate limits, presence (`REDIS_URL`) |
| `/1` | **JuiceFS metadata** — durable, must be backed up |
| `/2` | Celery broker |
| `/3` | Celery result backend |

Because `/1` holds filesystem metadata, this Redis is *not* a disposable
cache. `REDIS_MAXMEMORY` defaults to `0` (unlimited) with `noeviction` — if
you bound memory, never use an eviction policy that could evict JuiceFS keys;
move JuiceFS metadata to its own Redis first.

## MinIO

MinIO provides S3-compatible object storage for uploaded files, document
assets, generated media artifacts, and JuiceFS data blocks (bucket configured
via `MINIO_BUCKET` / `JUICEFS_BUCKET`).

## JuiceFS

JuiceFS gives Manor AI a POSIX filesystem per entity — the surface agent file
tools read and write — backed by Redis metadata (`/1`) and MinIO object
blocks. The `juicefs-init` one-shot service formats and mounts it on stack
start; the API and worker containers mount it at `MANOR_FS_ROOT`
(default `/mnt/manor`) over FUSE.

Consequences:

- Files are not readable directly from the MinIO bucket — blocks only make
  sense through JuiceFS with its metadata.
- Metadata (Redis `/1`) and blocks (MinIO) must be backed up as a pair.
- The workers stop with a 30-second grace period so the FUSE layer can flush
  its write cache to MinIO; avoid SIGKILL.

## Embeddings (Ollama)

Document RAG uses a local Ollama service with the `mxbai-embed-large` model
(1024 dimensions), preloaded by `ollama-init` into the `ollama_data` volume on
first boot. First startup downloads the model, so allow extra time. Without
the model, embedding calls fail and search degrades to text-only.

## Production Notes

- Use persistent volumes for every stateful service (`pg_data`, `redis_data`,
  `minio_data`, `ollama_data`).
- Monitor disk usage — generated media and document uploads accumulate in
  MinIO.
- Back up PostgreSQL, MinIO, and Redis `/1` together; keep filesystem and
  database snapshots from drifting apart.
