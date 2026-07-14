---
sidebar_position: 12
title: Troubleshooting
---

# Troubleshooting

## The Web App Does Not Load

Check containers:

```bash
docker compose ps
docker compose logs web --tail=100
docker compose logs api --tail=100
```

Confirm the web app is available at `http://localhost:18080`.

## Login Fails

Check:

- API logs.
- `JWT_SECRET_KEY` is set.
- Database migrations completed.
- Seed data exists for local demo login.

## API Cannot Connect to Database

Check `DATABASE_URL`, `DATABASE_URL_SYNC`, and PostgreSQL health:

```bash
docker compose logs postgres --tail=100
docker compose exec postgres pg_isready -U manor
```

## Worker Jobs Do Not Run

Check Redis and worker logs:

```bash
docker compose logs redis --tail=100
docker compose logs worker --tail=200
```

## File or Knowledge Uploads Fail

Check MinIO and JuiceFS:

```bash
docker compose logs minio --tail=100
docker compose logs juicefs-init --tail=100
docker compose logs api --tail=200
```

## Docusaurus Docs Build Fails

From the repository root:

```bash
cd docs-site
npm ci
npm run build
```

Broken links fail the build. Fix the link or update `sidebars.js`.
