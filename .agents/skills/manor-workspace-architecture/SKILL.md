---
name: manor-workspace-architecture
description: Use when changing, debugging, reviewing, documenting, or testing Manor Workspace behavior, especially when a change crosses chat, Strategist, Proposal, Task, Plan, Workflow, automation, approval, artifact, Knowledge, credits, lifecycle, or Blueprint boundaries.
---

# Manor Workspace Architecture

A Workspace is an operating boundary over several runtimes. It is not a Task,
a Workflow, a chat room, or a Blueprint. Any change must first identify which
runtime node owns the behavior and which downstream contracts it affects.

## Required reading

1. Read [architecture.md](references/architecture.md) completely.
2. Locate the requested change on one or more `WS-xx` nodes.
3. Read the code entries and state owners listed for those nodes.
4. Trace every outgoing edge from the changed nodes. State which edges are
   affected and which are intentionally unchanged.
5. Use [node-test-checklist.md](references/node-test-checklist.md) to run the
   node tests plus the contract tests for every crossed edge.

## Change protocol

Before editing code, write a compact impact statement containing:

- entry surface and trigger;
- owning node(s);
- persisted state read and written;
- approval and credit gates crossed;
- terminal success evidence;
- failure, retry, pause, resume, and deletion behavior;
- Blueprint portability impact: portable configuration, runtime state, or
  entity-level shared policy.

Do not infer behavior from UI text, a Blueprint description, or one service in
isolation. Models and execution services are authoritative. A prompt may guide
an agent, but it cannot replace a missing runtime edge or persistence contract.

## Boundary rules

- Proposal generates context-dependent Task instances. Blueprint never exports
  those Task rows, their Plans, Steps, Leases, runs, or artifacts.
- A Task Plan step and a Workflow step are different execution models. Do not
  move behavior between them without tracing approval, retry, output, and UI
  projection semantics.
- A local file is not a finished Workspace artifact until it is projected to a
  durable Knowledge `Document` and returned with a canonical reference.
- User chat, scheduled jobs, Strategist reviews, Tasks, and Workflows must all
  enter the same credit and governance boundaries before billable or external
  effects.
- Soft delete must stop new work immediately. Purge owns irreversible cleanup.
- Entity-scoped rows are not automatically owned by one Workspace. Export or
  mutate them only through an explicit entity-level contract.

## Completion gate

A Workspace change is complete only when:

1. the node's success invariant is proven;
2. crossed-edge contracts are tested;
3. pause/resume and terminal failure are covered where applicable;
4. artifacts are visible through their durable contract, not only on disk;
5. credits and approvals cannot be bypassed through another entry surface;
6. Blueprint install/export semantics remain accurate or are updated together.
