---
name: mcp_nango
description: Reach 200+ third-party apps through the Nango MCP. Use when the user wants to act on an app that has no dedicated Manor MCP but is available via Nango — discover the provider, confirm a connection exists, then call its API through the authenticated proxy.
version: 1.0.0
---

# Nango (200+ apps) Runtime Skill

Use this skill to reach apps that **don't have a dedicated Manor MCP** through Nango's authenticated proxy (`mcp__nango__*`). Unlike a typed MCP, Nango has no per-app tools — it gives you a generic authenticated HTTP proxy, so **you** must know the provider's REST API.

## When To Use

Use Nango when the user wants to act on a SaaS app that Manor doesn't expose as its own MCP, but which Nango supports. If a dedicated MCP exists for that app (Gmail, Slack-equivalents, Shopify, etc.), prefer it — it's typed and safer.

## Connection

Nango holds OAuth connections per provider. The flow is always **discover → verify → proxy**:
1. `nango_list_providers` — which integrations this Nango server supports.
2. `nango_list_connections` — which providers actually have an active OAuth connection right now.
3. `nango_proxy` — make an authenticated HTTP request (method, path, query, body) to a connected provider's API.

If the target provider isn't in `nango_list_connections`, stop and tell the user to connect it first — don't attempt a proxy call without a connection.

## Core Tools

- `nango_list_providers` — catalog of available integrations.
- `nango_list_connections` — active OAuth connections (provider + connection id).
- `nango_proxy` — authenticated HTTP call to a provider API (you supply method/endpoint/params; Nango injects auth).

## Common Recipes

**Act on a Nango-only app**
1. `nango_list_connections` → confirm the provider is connected (note the connection/provider id).
2. Determine the provider's REST endpoint from its public API docs (method, path, required fields).
3. `nango_proxy` with that request. 4. Read the response; for writes, confirm with the user first.

## Guardrails

- **The proxy can call ANY endpoint on a connected provider — including destructive ones.** Treat every non-GET (`POST`/`PUT`/`PATCH`/`DELETE`) as high-impact: confirm the provider, endpoint, and payload with the user before sending.
- **Verify a connection exists** (`nango_list_connections`) before proxying; never guess a connection/provider id.
- Be conservative with endpoints you're unsure about — prefer GET to inspect before any write; you own correctness since Nango won't validate the provider's schema for you.
- Don't expose secrets/tokens; Nango injects auth — never put credentials in the request yourself.

## Edge Cases & Errors

- Provider supported (`nango_list_providers`) but not connected (`nango_list_connections`) → ask the user to connect it; supported ≠ connected.
- A 4xx from the proxy is usually a wrong endpoint/payload against the provider API — re-check that provider's docs rather than retrying blindly.
- Rate limits and pagination are the provider's, not Nango's — handle per that API.
