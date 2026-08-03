---
title: Skills and Tools
---

# Skills and Tools

Tools are executable capabilities. Skills are instruction packages that teach an
agent when and how to use capabilities.

## Tools

Tools can read files, call APIs, search the web, query knowledge (RAG),
manage documents and tasks, schedule jobs, drive a browser session, generate
media, run code in the sandbox, and interact with connected integrations
through MCP servers.

Tool access is governed by:

- Agent configuration (the agent's bound tool set).
- Runtime policy.
- Workspace permissions.
- HITL approval rules.

Two search-shaped tools are worth distinguishing: `search_documents` finds
files by name and metadata; `rag` retrieves content evidence from inside
documents. Agents are steered to the right one automatically, but knowing the
split helps when reading execution logs.

## Skills

A skill is a reusable recipe — "how to produce a weekly digest",
"how to triage a support email" — that loads into an agent's context only
when relevant, keeping prompts focused.

A skill usually contains a `SKILL.md` file (YAML frontmatter plus the
instruction body) and optional assets or scripts. Scripts execute in the
[sandbox](../operations/sandbox.md), not in the application processes.

Manage skills under **Skills** in the sidebar or via the API:

| Endpoint | Purpose |
| --- | --- |
| `GET /api/v1/skills` / `POST /api/v1/skills` | List / create |
| `POST /api/v1/skills/generate` | Generate a skill from a description |
| `POST /api/v1/skills/install-github` | Install a skill from a GitHub repository |

Skills also power [automations](automations.md): when you schedule an agent
job, Manor freezes the prompt into a generated execution skill so every run
behaves consistently.

## Writing Good Skills

- Name the use case clearly.
- Explain required inputs.
- Describe tool usage boundaries.
- Include examples for common workflows.
- Avoid broad permissions when narrower tools are available.
