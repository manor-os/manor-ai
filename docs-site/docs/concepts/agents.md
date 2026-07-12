---
title: Agents
---

# Agents

Agents are reusable AI workers with instructions, tool access, memory context,
and runtime governance.

## What an Agent Contains

- System instructions and behavior rules.
- Tool bindings that define what the agent can call.
- Optional skills that package domain-specific instructions and workflows.
- Model preferences and routing behavior.
- Workspace and user context.

## Tool Scope

Agents should receive the smallest tool set that can complete their work. Tool
scope is enforced at runtime and surfaced in the UI so operators can understand
what an agent is allowed to do.

## Runtime Loop

During a conversation or task run, the agent can:

1. Read context from the conversation, workspace, and knowledge tools.
2. Call allowed tools.
3. Request human approval for sensitive actions.
4. Produce user-visible results and artifacts.

## Good Agent Design

- Give agents clear ownership.
- Bind only necessary tools.
- Prefer explicit HITL requirements for irreversible actions.
- Keep instructions short enough to audit.
- Test agents against real workflows before broad use.
