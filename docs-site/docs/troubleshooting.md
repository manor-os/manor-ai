---
sidebar_position: 12
title: Troubleshooting
---

# Troubleshooting

Work top-down: `docker compose ps` first — most issues are one unhealthy
service, and every service defines a healthcheck.

## The Web App Does Not Load

```bash
docker compose ps
docker compose logs web --tail=100
docker compose logs api --tail=100
```

Confirm the web app is available at `http://localhost:18080`. If `web` is up
but the page errors, check the browser console for failing `/api` calls — the
web container proxies `/api` to the API container.

## Login Fails

Check:

- API logs (`docker compose logs api --tail=200`).
- `JWT_SECRET_KEY` is set and unchanged since sessions were issued (rotating
  it invalidates all sessions).
- Database migrations completed (they run on API startup — look for Alembic
  lines in the API log).
- Seed data exists for the local demo login (`demo@manor.local`).

## API Cannot Connect to Database

Check `DATABASE_URL`, `DATABASE_URL_SYNC`, and PostgreSQL health:

```bash
docker compose logs postgres --tail=100
docker compose exec postgres pg_isready -U manor
```

If the API log shows pool-timeout errors under load instead, revisit the
connection-sizing note in [Configuration](configuration#database).

## Worker Jobs / Scheduled Jobs Do Not Run

```bash
docker compose logs redis --tail=100
docker compose logs worker --tail=200
docker compose logs worker-work --tail=200
```

Both workers must be running: `worker` schedules and dispatches (Celery
beat), `worker-work` executes long plan steps. If scheduled jobs fire but
steps never start, `worker-work` is the one to check.

## Agent Answers But Search Finds Nothing

Knowledge search depends on the embedding runtime:

```bash
docker compose logs ollama --tail=50
docker compose logs ollama-init --tail=50
```

`ollama-init` must have completed its one-time `mxbai-embed-large` download.
Until it does, RAG degrades to text-only matching. First boot on a slow
connection can take several minutes.

## File or Knowledge Uploads Fail

Check MinIO and JuiceFS:

```bash
docker compose logs minio --tail=100
docker compose logs juicefs-init --tail=100
docker compose logs api --tail=200
```

`juicefs-init` must have exited successfully; the API and workers mount the
filesystem at `/mnt/manor`. A wedged FUSE mount after an unclean stop shows
up as I/O errors in API/worker logs — restart the affected containers.

## Webhooks / Channel Messages Never Arrive

- `PUBLIC_BASE_URL` must be a URL the provider's servers can actually reach
  (HTTPS in production). For local testing use a tunnel and update it.
- Telegram: with a non-HTTPS `PUBLIC_BASE_URL`, `TELEGRAM_MODE=auto` falls
  back to polling — check API logs for the poller instead of a webhook.
- Meta/Discord/Twilio webhooks fail silently on signature mismatch — confirm
  the app secrets in `.env` match the provider console.

## Stale Frontend After an Upgrade

If the UI errors right after a deploy, the browser may hold a stale bundle.
Hard-refresh (`Cmd+Shift+R` / `Ctrl+Shift+R`). Self-hosted operators should
confirm the `web` image was actually rebuilt
(`docker compose build web && docker compose up -d web`).

## Emergency: Deployment Overloaded

Set `DEGRADED_MODE=true` and restart the API to shed expensive routes
(streaming chat, sandbox, media generation, large uploads) while keeping
login and reads alive — see
[Configuration](configuration#degraded-mode).

## Docusaurus Docs Build Fails

From the repository root:

```bash
cd docs-site
npm ci
npm run build
```

Broken links fail the build. Fix the link or update `sidebars.js`.
