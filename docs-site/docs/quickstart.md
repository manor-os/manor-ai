---
sidebar_position: 2
title: Quick Start
---

# Quick Start

This path gets Manor AI running on one machine with Docker Compose.

## Prerequisites

- Docker Compose v2
- Python 3.11 or newer
- Node.js 20 or newer
- Git

## Clone

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
```

The default `.env.example` is suitable for local evaluation. Before any shared
or internet-accessible deployment, change secrets such as `JWT_SECRET_KEY`,
MinIO credentials, and any OAuth client secrets.

## Start the Stack

```bash
docker compose up --build -d
```

Open:

```text
http://localhost:18080
```

The self-hosted stack seeds a local demo account by default:

```text
demo@manor.local / manor-demo
```

## Configure Model Keys

Manor AI is BYOK in self-hosted mode. After signing in, open Settings and add
the provider keys your workspace should use. Keep provider keys out of source
control and `.env` files when possible.

## Verify Services

```bash
docker compose ps
docker compose logs api --tail=100
docker compose logs worker --tail=100
docker compose logs web --tail=100
```

For a deeper view of the stack, see [Docker Compose](docker-compose.md).

## Next Steps

- Read [Configuration](configuration.md) for production-minded settings.
- Review [Backup and Restore](operations/backup-restore.md) before storing
  important data.
