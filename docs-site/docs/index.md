---
sidebar_position: 1
title: Overview
---

# Manor AI

Manor AI is a self-hosted AI workspace runtime for teams that want agents,
tools, documents, workflows, and integrations under their own control.

The repository is designed for operators who want to run Manor AI on their own
infrastructure with user-provided model keys. It includes the API server, React
web app, worker runtime, sandbox service, workspace
data model, knowledge tools, document tooling, and integration surfaces needed
for a self-hosted deployment.

## What You Can Build

- AI workspaces with chat, tasks, documents, knowledge, goals, and workflows.
- Custom agents with scoped tools and human-in-the-loop approval.
- BYOK model routing for OpenAI-compatible, Anthropic, OpenRouter, and other
  supported provider paths.
- Self-hosted integrations through webhooks, OAuth providers, and Nango.
- Internal operational workflows that need auditability and data isolation.

## Scope

This codebase focuses on self-hosted operation. It does not require hosted
Manor AI services to boot, create workspaces, configure model keys, run agents,
use the sandbox, or manage documents and knowledge.

## Start Here

1. Follow the [Quick Start](quickstart.md) to run the stack locally.
2. Review [Configuration](configuration.md) before exposing Manor AI to users.
3. Read [Core Concepts](concepts/agents.md) to understand agents, skills, tools,
   and HITL governance.
4. Use [Troubleshooting](troubleshooting.md) when a service fails to boot.

## Repository

- Source: [github.com/manor-os/manor-ai](https://github.com/manor-os/manor-ai)
- License: Manor Sustainable Use License 1.0
- Security reports: use GitHub private vulnerability reporting for the repository.
