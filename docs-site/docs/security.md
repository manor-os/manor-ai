---
sidebar_position: 13
title: Security
---

# Security

This page summarizes operational security guidance for self-hosted
deployments. See `SECURITY.md` in the repository root for vulnerability
reporting.

## Before Production Use

- Replace all default secrets: `JWT_SECRET_KEY`, the PostgreSQL password,
  `MINIO_ACCESS_KEY` / `MINIO_SECRET_KEY` (and the matching `JUICEFS_*`
  credentials), and the Vault token.
- Use HTTPS for `APP_URL` and `PUBLIC_BASE_URL`. OAuth callbacks and most
  channel webhooks require it.
- Restrict PostgreSQL, Redis, MinIO, Vault, Ollama, and the sandbox service
  to the private Docker network. Only the `web` service (and, if used, your
  reverse proxy) should be reachable from outside.
- Enable rate limiting (`RATE_LIMIT_ENABLED`, `CHAT_RATE_LIMIT_ENABLED`,
  with `REDIS_RATE_LIMIT_ENABLED` when running multiple API workers).
- Configure backups and test restores
  ([Backup and Restore](operations/backup-restore.md)).
- Require [HITL approval](concepts/hitl-governance.md) for sensitive agent
  actions and limit agent tool scopes to what each agent actually needs.
- Rotate provider credentials periodically.
- Delete or change the seeded demo account before exposing a deployment.

## Secrets

Never commit `.env`, provider keys, OAuth secrets, webhook secrets, database
passwords, or production logs.

At rest, integration credentials are encrypted through a Vault transit
backend (`VAULT_TOKEN`, `VAULT_TRANSIT_KEY`). The Compose stack ships a local
development Vault; for production either protect its storage or point Manor
at an external Vault.

Model provider keys are BYOK and entered through Settings — they live in your
deployment's database (encrypted), never in source control or images.

## Session and Account Security

- Sessions are JWTs signed with `JWT_SECRET_KEY`; expiry defaults to 24 hours
  (`JWT_EXPIRE_MINUTES`).
- Users can enable two-factor authentication (TOTP) and review active
  sessions under **Settings → Security**.
- API keys for programmatic access are managed under **Settings → API Keys**;
  scope them narrowly and rotate on personnel changes.

## Public Endpoints

A few routes are intentionally unauthenticated; know them when you expose a
deployment:

| Endpoint | Purpose | Protection |
| --- | --- | --- |
| `POST /api/v1/workflows/webhook/{token}` | Workflow webhook trigger | The token is the binding's shared secret; rotate by recreating the binding. |
| `GET/POST /api/v1/channels/...` webhooks | Inbound channel messages | Provider signature checks (Meta HMAC, Discord Ed25519, Twilio signatures) or provider-specific challenge flows. |
| `GET /api/v1/calendar-settings/public/booking-links/...` | Public booking pages | Read-only availability; bookings create tasks, no data exposure. |
| Shared document / public chat token links | Explicit sharing | Capability tokens; treat links as secrets. |

## Sandbox and Agent Execution

Agent code and shell execution run in the isolated
[sandbox service](operations/sandbox.md) with memory, CPU, PID, and timeout
limits and a read-only root. Keep sandbox access internal, leave
`SHELL_SANDBOX_ENABLED` off if you don't need shell tools, and pair
execution-capable agents with explicit governance rules.
