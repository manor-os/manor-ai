---
title: Integrations Overview
---

# Integrations Overview

Manor AI supports self-hosted integrations through provider credentials,
webhooks, OAuth, and optional Nango.

## Integration Types

- Webhooks for inbound and outbound events.
- OAuth-based provider connections.
- API-key-based tools.
- Nango-backed SaaS connectors for providers supported by Nango.

## Credentials

Store credentials through the application settings or secret-backed integration
flows. Do not commit provider credentials into source control.

## Public URLs

Providers need a stable HTTPS callback URL. Set `PUBLIC_BASE_URL` to the
external URL for your deployment.

## Local Testing

For local webhook testing, use a tunnel and update `PUBLIC_BASE_URL` for the
duration of the test.
