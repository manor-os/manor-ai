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

<img
  src="../img/manor-why-task-evidence.png"
  alt="Manor AI task detail showing task status, priority, brief, and workspace metadata"
/>

## Execution Evidence

Agent work should remain inspectable after the conversation moves on. Task runs
can show plan steps, step output, generated artifacts, waiting human input, and
status changes so operators can understand what happened and resume work from a
known state.

<img
  src="../img/manor-why-task-evidence2.png"
  alt="Manor AI task run steps showing completed agent output and a waiting human approval step"
/>

<img
  src="../img/manor-why-task-evidence3.png"
  alt="Manor AI task activity timeline showing execution steps, reminders, and status changes"
/>

## Good Agent Design

- Give agents clear ownership.
- Bind only necessary tools.
- Prefer explicit HITL requirements for irreversible actions.
- Keep instructions short enough to audit.
- Test agents against real workflows before broad use.
