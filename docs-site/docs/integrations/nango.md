---
title: Nango
---

# Nango

Nango is an optional self-hosted OAuth and API connector service. Manor AI can
use it to connect many SaaS providers without building every OAuth flow from
scratch.

## Starting Nango

The Compose file includes Nango services behind the `nango` profile.

```bash
docker compose --profile nango up -d nango-postgres nango-server
```

Open the Nango UI, create or copy keys, and place them in your Manor AI
configuration.

## When to Use Nango

Use Nango when:

- A provider requires OAuth.
- You want a reusable connector across workspaces.
- You prefer a self-hosted integration hub.

Use direct API keys or webhooks when the provider flow is simple.

## Production Operations

Treat each environment as an independent Nango installation:

- Use a separate PostgreSQL database and a separate encryption/API key set for
  every environment. Do not copy a test database, provider rows, or keys into
  production.
- Give OAuth callbacks a stable HTTPS origin. Expose only the routes required
  for OAuth, and keep the management API private.
- Back up the Nango PostgreSQL database on a schedule and verify that each
  backup can be read by the matching PostgreSQL restore tooling.
- If a Nango key or provider credential may have leaked, rotate it and ask
  users to authorize the provider again. Do not reuse the exposed key or move
  an existing connection record between environments.
