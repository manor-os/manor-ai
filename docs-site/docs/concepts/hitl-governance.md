---
title: HITL Governance
---

# Human-in-the-Loop Governance

Human-in-the-loop (HITL) governance lets operators require approval before an
agent performs sensitive actions.

## Why HITL Matters

Agents can draft, search, summarize, and prepare work quickly. Some actions
still need explicit human confirmation:

- Sending external messages.
- Publishing public content.
- Writing files in sensitive locations.
- Triggering integrations with side effects.
- Running shell commands.

## Action Classes

Manor AI policies can treat actions differently:

- Allowed: the agent can proceed.
- HITL required: the agent pauses for confirmation.
- Blocked: the action is not available.

## Operational Guidance

Start conservative. Require HITL for irreversible or externally visible
actions. After you trust a workflow and account boundary, selectively loosen
approval requirements for low-risk automation.

## Auditability

Approval prompts and tool calls should leave enough evidence for a human to
understand what was requested, who approved it, and what happened next.
