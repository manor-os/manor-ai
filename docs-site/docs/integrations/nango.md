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
