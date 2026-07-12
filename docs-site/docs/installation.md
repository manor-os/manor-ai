---
sidebar_position: 3
title: Installation
---

# Installation

Manor OS supports two common self-hosted workflows:

- Docker Compose for a production-like single-host deployment.
- Local development with Python, Node.js, PostgreSQL, Redis, and MinIO.

## Docker Compose

Docker Compose is the recommended first install path.

```bash
git clone https://github.com/manor-os/manor-os.git
cd manor-os
cp .env.example .env
docker compose up --build -d
```

This starts the API, web frontend, worker, PostgreSQL, Redis, MinIO, sandbox,
browser-runner, and supporting services.

## Local Development

Use this path when changing code.

```bash
cp .env.example .env
pip install ".[dev]"
cd apps/web
npm ci
cd ../..
./scripts/dev.sh infra
./scripts/dev.sh init
```

Run the API and web app in separate terminals:

```bash
./scripts/dev.sh api
./scripts/dev.sh web
```

Optional worker:

```bash
./scripts/dev.sh worker
```

## Database Initialization

`./scripts/dev.sh init` runs migrations and seed logic. Docker deployments run
the same initialization path through the container entrypoint.

## Updating an Existing Install

For source installs:

```bash
git pull
pip install ".[dev]"
cd apps/web && npm ci && cd ../..
docker compose up --build -d
```

Review release notes before applying updates to shared environments.
