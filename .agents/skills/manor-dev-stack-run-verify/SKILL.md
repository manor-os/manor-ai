---
name: manor-dev-stack-run-verify
description: Use when starting, verifying, debugging, or repairing the local Manor self-hosted dev stack, Docker Compose services, seeded demo login, API/web/worker health, Postgres/Redis/MinIO/sandbox dependencies, or runtime readiness.
---

# Manor Dev Stack Run Verify

Use this skill to bring up the local self-hosted Manor stack and prove it is usable.

## Default local model

- Compose is the canonical self-hosted path: `docker compose up --build -d`.
- Default browser entry: `http://localhost:18080`.
- Seeded local demo account: `demo@manor.local / manor-demo`.
- Core services: web, API, worker, PostgreSQL + pgvector, Redis, MinIO, Vault, JuiceFS init, sandbox service, and Ollama local embeddings.

## Safe startup flow

1. Inspect `.env` presence. If missing, copy `.env.example` to `.env` only when the user asked to start or initialize local dev.
2. Check for dirty worktree before destructive cleanup. Never remove volumes or reset data without explicit permission.
3. Start or refresh services:

```bash
docker compose up --build -d
docker compose ps
```

4. Inspect unhealthy containers with:

```bash
docker compose logs --tail=200 <service>
```

## Verification probes

Use available probes in this order:

```bash
docker compose ps
curl -fsS http://localhost:18080 >/dev/null
curl -fsS http://localhost:8010/health || curl -fsS http://localhost:18080/health
```

Then run focused tests for the area being changed:

```bash
PYTHONPATH=. .venv/bin/python -m pytest tests/test_health.py tests/test_oss_demo_account.py -q --tb=short -p no:warnings
```

For frontend changes:

```bash
npm --prefix apps/web ci
npm --prefix apps/web run build
```

## Troubleshooting order

1. Port conflict: identify owner with `lsof -i :18080` or the failing port.
2. DB/Redis health: inspect compose health status and service logs.
3. Migration/init issues: check API logs before editing migrations.
4. Sandbox issues: verify sandbox container and network reachability before changing tool runtime code.
5. Embedding/RAG issues: inspect `docker compose logs ollama ollama-init --tail=100` and confirm the `mxbai-embed-large` model is present.
6. Frontend proxy issues: compare browser URL, API base URL, and compose ports.

## Report back

Report service health, URL, login readiness, failed probes, and exact logs/commands used. Name any step skipped because it would be destructive.
