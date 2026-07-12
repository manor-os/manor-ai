---
sidebar_position: 13
title: Security
---

# Security

This page summarizes operational security guidance. See `SECURITY.md` in the
repository root for vulnerability reporting.

## Before Production Use

- Replace all default secrets.
- Use HTTPS for `APP_URL` and `PUBLIC_BASE_URL`.
- Restrict database, Redis, MinIO, sandbox, and browser-runner to private
  networks.
- Configure backups.
- Require HITL for sensitive agent actions.
- Limit agent tool scopes.
- Rotate provider credentials periodically.

## Secrets

Never commit `.env`, provider keys, OAuth secrets, webhook secrets, database
passwords, or production logs.

## Browser Automation

Browser-backed automations can interact with external accounts. Treat connected
sessions as sensitive credentials. Use HITL for externally visible actions such
as publishing, sending messages, or changing account state.

## Sandbox

Keep sandbox access internal. Enable shell tools only for trusted workspaces and
pair them with explicit governance.
