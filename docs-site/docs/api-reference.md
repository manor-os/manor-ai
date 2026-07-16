---
sidebar_position: 10
title: API Reference
---

# API Reference

Manor AI exposes the same HTTP API used by the web app. The interactive
OpenAPI views are still the source of truth for exact request and response
schemas, but this page gives operators and integrators a readable map of the
public surface.

## Local URLs

When running through Docker Compose:

```text
http://localhost:18080/api/docs
http://localhost:18080/api/redoc
http://localhost:18080/api/openapi.json
```

When running the API directly:

```text
http://localhost:8000/api/docs
http://localhost:8000/api/redoc
http://localhost:8000/api/openapi.json
```

## Authentication

Most `/api/v1/*` routes require a bearer token:

```http
Authorization: Bearer <access_token>
```

Create a session token with `POST /api/v1/auth/login`, or sign in through the
web app and inspect the OpenAPI docs with the same backend. Model provider API
keys are different: they are BYOK credentials used by the agent runtime and are
managed with `/api/v1/api-keys` or the Settings UI.

```bash
curl -sS http://localhost:18080/api/v1/auth/login \
  -H "Content-Type: application/json" \
  -d '{"email":"demo@manor.local","password":"manor-demo"}'
```

## Core Resources

| Area | Primary endpoints | Use when you want to |
| --- | --- | --- |
| Auth and profile | `POST /api/v1/auth/login`, `GET /api/v1/auth/me`, `GET /api/v1/entities/me` | Sign in, inspect the current user, and manage the current entity |
| Workspaces | `GET /api/v1/workspaces`, `POST /api/v1/workspaces`, `GET /api/v1/workspaces/{workspace_id}` | Create operating workspaces, update workspace metadata, and read workspace dashboards |
| Workspace runtime | `GET /api/v1/workspaces/{workspace_id}/operating-model`, `GET /api/v1/workspaces/{workspace_id}/governance`, `GET /api/v1/workspaces/{workspace_id}/activity`, `GET /api/v1/workspaces/{workspace_id}/capabilities` | Configure how agents, goals, rules, and approvals behave inside a workspace |
| Chat | `POST /api/v1/chat/message`, `POST /api/v1/chat/stream`, `GET /api/v1/chat/conversations` | Send messages to the agent runtime, attach context, stream responses, and manage conversations |
| Workspace chat | `GET /api/v1/workspaces/{workspace_id}/chat/messages`, `POST /api/v1/workspaces/{workspace_id}/chat/messages` | Post and resolve messages in a workspace-scoped thread |
| Agents | `GET /api/v1/agents`, `POST /api/v1/agents`, `POST /api/v1/agents/generate`, `GET /api/v1/agents/{agent_id}/tools` | Create agents, generate agents from prompts, and attach tools |
| Skills | `GET /api/v1/skills`, `POST /api/v1/skills`, `POST /api/v1/skills/generate`, `POST /api/v1/skills/install-github` | Manage reusable skills available to agents |
| Tasks | `GET /api/v1/tasks`, `POST /api/v1/tasks`, `GET /api/v1/tasks/{task_id}` | Track work, approvals, comments, automation logs, and task state |
| Goals and plans | `GET /api/v1/goals`, `POST /api/v1/goals`, `GET /api/v1/plans`, `GET /api/v1/executions` | Define objectives, run plans, and inspect agent execution state |
| Documents | `GET /api/v1/documents`, `POST /api/v1/documents/upload`, `GET /api/v1/shared-doc/{token}` | Upload, create, share, and permission documents |
| Integrations | `GET /api/v1/integrations/mcp-servers`, `POST /api/v1/integration-sessions/start`, `GET /api/v1/webhooks` | Connect MCP servers, external accounts, OAuth/Nango flows, and outbound webhooks |
| Workers and sandbox | `GET /api/v1/workers`, `POST /api/v1/workers/heartbeat`, `POST /api/v1/workspaces/sandbox` | Register workers and run sandbox-backed execution where configured |
| Operations | `GET /health`, `GET /health/ready`, `GET /health/deep`, `GET /api/v1/backup/summary`, `GET /api/v1/usage/summary` | Monitor readiness, export backup data, and inspect usage |

The public OpenAPI schema currently contains hundreds of routes because the
web app is API-first. Start with the areas above; use Swagger or ReDoc when you
need the exact field-level contract.

## Common Calls

### Sign in and keep the token

```bash
TOKEN="$(
  curl -sS http://localhost:18080/api/v1/auth/login \
    -H "Content-Type: application/json" \
    -d '{"email":"demo@manor.local","password":"manor-demo"}' \
    | jq -r .access_token
)"
```

### List workspaces

```bash
curl -sS http://localhost:18080/api/v1/workspaces \
  -H "Authorization: Bearer $TOKEN"
```

### Create a workspace

```bash
curl -sS http://localhost:18080/api/v1/workspaces \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Customer Support Operations",
    "description": "Triage customer requests and escalate sensitive work.",
    "category": "operations"
  }'
```

### Send a non-streaming chat message

```bash
curl -sS http://localhost:18080/api/v1/chat/message \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "message": "Summarize the workspace priorities for today.",
    "workspace_context": true
  }'
```

### Stream an agent response

`/api/v1/chat/stream` returns Server-Sent Events and accepts
`multipart/form-data`, which lets callers include files and optional workspace
context.

```bash
curl -N http://localhost:18080/api/v1/chat/stream \
  -H "Authorization: Bearer $TOKEN" \
  -F "message=Draft a support triage plan" \
  -F "workspace_context=true"
```

### Add a model provider key

```bash
curl -sS http://localhost:18080/api/v1/api-keys \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "OpenRouter production",
    "provider": "openrouter",
    "api_key": "sk-or-...",
    "default_model": "openai/gpt-4.1",
    "is_default": true
  }'
```

The raw key is accepted on create or rotate only. List responses expose key
metadata and prefixes, not the secret value.

## Public and Embed Routes

Some routes are designed for unauthenticated external visitors after an
operator creates a share or public token:

| Surface | Endpoints |
| --- | --- |
| Shared documents | `/api/v1/shared-doc/{token}`, `/content`, `/download` |
| Shared folders | `/api/v1/shared-folder/{token}` |
| Public task review | `/api/v1/public/task`, `/update-status`, `/complete`, `/evaluate` |
| Public chat widgets | `/api/v1/public/chat/{token}`, `/session`, `/message`, `/message/stream`, `/embed.js` |
| Channel webhooks | `/api/v1/channels/*` callback endpoints |

Treat share tokens and channel webhook secrets as credentials. Rotate them if
they are exposed.

## Generate OpenAPI JSON

```bash
make openapi
```

This writes the OpenAPI document to `docs/openapi.json` in a development tree.
The generated schema is useful for inspection, contract tests, and client
generation.

```bash
npx openapi-typescript docs/openapi.json -o manor-api.d.ts
```

## Stability Notes

- Routes listed in **Core Resources** are the intended integration starting
  points for self-hosted deployments.
- Platform administration routes are not needed for normal workspace
  automation.
- Routes for cloud marketplace, billing, remote coding, and CLI distribution
  are not part of the public OSS runtime export.
- The OpenAPI schema is versioned with the repository. Regenerate clients after
  upgrading Manor AI.
