---
sidebar_position: 3
title: Installation
---

# Installation

Manor AI supports two common self-hosted workflows:

- Docker Compose for a production-like single-host deployment.
- Local development with Python, Node.js, PostgreSQL, Redis, and MinIO.

## Docker Compose

Docker Compose is the recommended first install path.

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
docker compose up --build -d
```

This starts the API, web frontend, worker, PostgreSQL, Redis, MinIO, sandbox, and supporting services.

Keep `DEPLOYMENT_MODE=oss`. A self-hosted deployment can contain multiple users
and Entities with separate permissions; this does not change the license's
restrictions on offering a hosted service to third parties. Configure your
Entity's model provider and BYOK credentials before running AI. Manor-managed
keys are never a fallback, including Gateway and voice calls.

SMTP, Nango/OAuth, Twilio, media providers and Sites hosting are optional.
Missing credentials disable the dependent operation, not the whole Workspace.
Sites require `MANOR_SITES_DOMAIN`, apex/wildcard DNS, and a reverse proxy with
TLS forwarding site hosts to the API; leave the domain empty to disable hosting.
Stable Flows are enabled even with `MANOR_ENV=prod`; explicit
`FLOWS_AVAILABLE=false` disables them. Experimental task engines remain opt-in.

### Optional Sites Proxy

Use a separate host from the authenticated application. For example, with
`MANOR_SITES_DOMAIN=sites.example.com`, point the wildcard DNS to your server
and obtain a certificate covering `*.sites.example.com`. A standalone Nginx
proxy on that server can use:

```nginx
server {
    listen 443 ssl;
    server_name *.sites.example.com;
    ssl_certificate /etc/ssl/sites/fullchain.pem;
    ssl_certificate_key /etc/ssl/sites/privkey.pem;
    location / {
        proxy_set_header Host $host;
        proxy_set_header X-Forwarded-Host $host;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_pass http://127.0.0.1:8010/api/v1/site-host/;
    }
}
```

The trailing slash maps `/` and `/__manor/*` to the Sites API. Do not apply the
application's `robots.txt` or global noindex headers to site hosts. Custom
domains additionally require registration in Sites, matching DNS and their
own TLS certificates/proxy host configuration. Unknown/unpublished sites 404.

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
