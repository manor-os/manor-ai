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

Self-hosted integrations use operator-owned accounts. An unconfigured optional
provider reports a setup requirement; it never falls back to Manor credentials.
Entity permissions and tool approvals apply even when commercial quota gates
are permissive. Installing or delivering a Blueprint does not copy credentials
or grant provider access to the recipient.

### International Lark OAuth

An administrator registers an international Lark OAuth app and configures its
credentials for the deployment; each user separately connects and consents to
their own Lark account. Reconnecting must use the same account and app. Lark
credentials and connected-account tokens are never portable through a
Blueprint. Legacy Feishu app and bot connections remain separate from this
international Lark OAuth flow. See [Configuration](../configuration#international-lark-oauth)
for the callback URL and required scopes.

### Model Gateway

`POST /api/v1/llm/chat/completions` accepts normal or streaming requests using
the target Entity's configured BYOK model. A requested model must match that
configuration. A user access token retains the user's selected Entity and
active membership. For a machine client, use the existing startup bootstrap:
set `MANOR_OAUTH_CLIENT_<SLUG>_SECRET`, `_REDIRECT_URIS` and
`_RESTRICTED_ENTITY_ID` in the deployment environment. Supply
`X-Manor-Client-Id`, `X-Manor-Client-Secret` and the matching
`X-Manor-Entity-Id`. `X-Manor-User-Id` is optional; when supplied, that user must
be an active member of the target Entity. An Entity header is not authorization.

`GET /api/v1/llm/credit` only checks the commercial credit gate. An OSS allow
response does not establish provider readiness or remove execution budgets.

## Public URLs

First-party OAuth providers need a stable callback URL based on `APP_URL`:
`{APP_URL}/api/v1/integrations/oauth/{server_key}/callback`. Inbound webhook
providers need `PUBLIC_BASE_URL` to point at the externally reachable API URL.

## Local Testing

For local webhook testing, use a tunnel and update `PUBLIC_BASE_URL` for the
duration of the test.
