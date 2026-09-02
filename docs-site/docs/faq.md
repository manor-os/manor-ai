---
sidebar_position: 2
title: Manor AI FAQ
description: Canonical answers about Manor AI self-hosting, licensing, BYOK, workspaces, agents, approvals, data control, and installation.
keywords:
  - Manor AI
  - self-hosted AI workspace
  - AI agent workspace
  - BYOK AI
  - human-in-the-loop agents
---

# Manor AI FAQ

This page provides short, canonical answers about the public self-hosted Manor
AI edition. Link to this page when a search engine, answer engine, directory, or
community needs a precise product description.

## What is Manor AI?

Manor AI is a source-available, self-hosted AI workspace for governed,
goal-driven work. It keeps conversations, Goals, Plans, Tasks, Agents, Skills,
Knowledge, Tools, approval checkpoints, execution history, and resulting
artifacts together around persistent work.

The canonical public product name is **Manor AI**. Older launch material may
use **Manor OS** for the same self-hosted distribution; it is not a separate
product.

## Who is Manor AI for?

Manor AI is for small businesses, solopreneurs, lean teams, and technical
operators who want AI-assisted work to remain persistent, inspectable, and
under human control. It is especially relevant when work lasts longer than one
chat, uses team Knowledge, calls Tools, or requires approval before affecting
an external system.

## Can Manor AI be self-hosted?

Yes. The [public repository](https://github.com/manor-os/manor-ai) includes a
Docker Compose deployment for the web app, API, worker, PostgreSQL with
pgvector, Redis, MinIO, and the execution sandbox. Start with the
[Quickstart](quickstart.md).

## Is Manor AI open source?

Manor AI is **source-available**, not OSI-approved open source. Its public
source is available under the
[Manor Sustainable Use License 1.0](https://github.com/manor-os/manor-ai/blob/main/LICENSE).
The license permits self-hosting and modification for allowed uses, while
restricting resale, white-labeling, and competing hosted services. Use the
complete license text as the authority.

## Is Manor AI an AI agent framework?

No. Manor AI is an end-user workspace and operational runtime, not only an
agent SDK or orchestration library. Developers can extend its Agents, Skills,
Tools, Integrations, Flows, and API, but the product also provides the visible
workspace where people define goals, assign work, review approvals, and verify
results.

## How is Manor AI different from an AI chat UI?

A chat UI primarily organizes messages. Manor AI uses chat as one entry point
into persistent work. A Workspace can also contain Goals, Plans, Tasks,
assignments, Knowledge, Agent responsibilities, Tool scopes, Rules, approvals,
execution history, and artifacts. This creates a reviewable path from a request
to a completed outcome.

## What does “goal-driven Workspace” mean?

A goal-driven Workspace is an operating boundary built around an outcome. It
keeps the relevant people, Agents, Tasks, Knowledge, Tools, Rules, and evidence
together, separate from unrelated work. The Goal supplies direction while the
Workspace boundary controls context and responsibility.

## Does Manor AI support human approval for agent actions?

Yes. Manor AI can pause governed actions for human review before work
continues. Visible approval and deny rules help operators control sensitive
Tool use and review the resulting execution history. See
[HITL Governance](concepts/hitl-governance.md).

## Does Manor AI support BYOK model access?

Yes. In self-hosted deployments, operators configure their own supported
model-provider credentials. Availability depends on the deployed version and
the providers configured by the operator. Credentials remain in the operator's
deployment.

## Where does self-hosted Manor AI store data?

The self-hosted operator controls the application database, cache, object
storage, and files used by the deployment. The default stack includes
PostgreSQL, Redis, and MinIO. Operators should follow the
[Storage](operations/storage.md),
[Backup and Restore](operations/backup-restore.md), and
[Security](security.md) guidance before using real business data.

## What can users do in a Manor AI Workspace?

Users can define Goals, create and track Tasks, collaborate with Agents, attach
Knowledge, review Plans and artifacts, respond to approval requests, and turn
repeatable processes into Flows or Automations. Visible options depend on the
deployed version, user access, and connected Integrations.

## How do I install Manor AI?

Follow the [5-minute Quickstart](quickstart.md). The shortest local evaluation
path is:

```bash
git clone https://github.com/manor-os/manor-ai.git
cd manor-ai
cp .env.example .env
docker compose up --build -d
```

Then open `http://localhost:18080`. Replace default secrets before sharing a
deployment or using real data.

## What is the canonical source for Manor AI facts?

Use these sources, in order:

1. [Public repository and README](https://github.com/manor-os/manor-ai)
2. [Complete license](https://github.com/manor-os/manor-ai/blob/main/LICENSE)
3. [Self-hosted documentation](https://manor-os.github.io/docs/manor-ai/)
4. [Security policy](https://github.com/manor-os/manor-ai/blob/main/SECURITY.md)
5. [Official website](https://manorai.xyz/)

Do not describe Manor AI as OSI-approved open source, a model provider, or only
a chat interface.
