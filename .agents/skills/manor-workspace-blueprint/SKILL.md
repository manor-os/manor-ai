---
name: manor-workspace-blueprint
description: Use when creating, exporting, hand-authoring, installing, upgrading, reverting, reviewing, or debugging any Manor Workspace Blueprint, especially when Workspace behavior differs from its Blueprint or when workflows, tasks, automations, artifacts, approvals, or runtime requirements are involved.
---

# Manor Workspace Blueprint

**REQUIRED BACKGROUND:** Use `manor-workspace-architecture` first. Map the
requested Blueprint change to Workspace runtime nodes and their cross-node
contracts before reading or changing `export_workspace()` or installation
logic. Blueprint is the WS-14 portability boundary, not the Workspace
architecture itself.

Treat a Blueprint as a portable projection of a real Workspace configuration,
not as a second workflow implementation. The authoritative lifecycle is:

```text
Workspace created/configured in Manor
  -> export_workspace()
  -> validated Blueprint payload
  -> install_blueprint()
  -> installed Workspace
  -> export again for semantic round-trip comparison
```

## Non-negotiable rules

1. Do not hand-edit a Blueprint as the primary way to design Workspace behavior.
   Configure a real Workspace first, export it, then make only deliberate
   portable edits.
2. Never claim install/export parity from `validate_payload()` alone. That
   validator checks shape and safety, not runtime equivalence.
3. Treat Workflow, Proposal-generated Task behavior, and Automation as separate contracts.
   Include all three in the review and in the round-trip evidence.
4. Distinguish portable configuration from runtime state. Do not export or
   promise portability for Task instances, task logs, workflow runs, agent
   executions, conversations, messages, leases, credentials, measurements, or
   generated artifacts unless the payload explicitly defines a safe portable
   representation.
5. Any non-empty payload section that the installer does not materialize is a
   release blocker. Either implement its installer/exporter mapping or mark it
   explicitly excluded and keep it empty.
6. Natural-language fields (`summary`, descriptions, prompts, operating-loop
   copy, acceptance text, and `not_included`) explain the structured contract;
   they must not introduce steps, tools, approvals, or deliverables absent from
   that contract.

## Required workflow

1. Apply `manor-workspace-architecture`, read its architecture and node test
   checklist, then read [runtime-contract.md](references/runtime-contract.md) and inspect the
   current exporter, installer, payload validator, and upgrade code.
2. Define the intended Workspace behavior as a structured inventory:
   operating model, agents/subscriptions, skills/knowledge, workflows, tasks,
   automations, goals/stats, governance, channels/sessions, and deliverables.
3. Create or update the real Workspace configuration. Do not start with a
   free-form Blueprint JSON unless the task is explicitly a migration.
4. Export with `export_workspace()` and run the portable payload validator.
5. Inspect the exported payload for source-row IDs, secrets, runtime state, and
   unsupported sections. Fail closed on any of them.
6. Install the payload into an isolated Workspace in both simulation and live
   modes when both modes are in scope. Record install todos and materialized
   IDs; an install with blocking todos is not equivalent.
7. Export the installed Workspace again. Normalize only generated identifiers,
   install timestamps, sandbox/provenance fields, and workspace-specific job
   suffixes. Compare the runtime-projected sections, including Workflow graphs,
   Automation triggers, artifact contracts, and approval scopes. Verify
   one-shot install declarations
   (`contract.variables`, unresolved channel/session requirements,
   `post_install_checks`, and `expected_baseline`) from install preview/todos and
   check results; they are not reconstructable from the installed Workspace.
8. Run the deterministic checker:

   ```bash
   ./.venv/bin/python .agents/skills/manor-workspace-blueprint/scripts/check_blueprint.py path/to/blueprint.json
   ```

   Compare source and installed exports with the semantic round-trip checker:

   ```bash
   ./.venv/bin/python .agents/skills/manor-workspace-blueprint/scripts/compare_roundtrip.py \
     source.json installed.json
   ```

9. Add or update a regression test for every parity rule. At minimum run the
   relevant `tests/test_blueprint_v11_e2e.py`, exporter/installer tests, and
   `git diff --check`.
10. Only after the runtime projection round-trip and the separate one-shot
    install checks pass may the Blueprint be published, seeded as built-in
    content, or used as an upgrade source.

## Failure policy

Stop and report the exact object and boundary when any of these occur:

- exported content cannot be installed;
- installed content cannot be exported back;
- a Workflow step or dependency changes;
- an entity-level Task category, SLA, or SLA-linked escalation policy is placed
  in a Workspace Blueprint;
- an Automation target resolves to a different Workflow/Agent;
- an explicitly portable configuration is dropped;
- an artifact/output contract changes;
- approval or governance scope becomes broader or narrower;
- human-readable Blueprint copy disagrees with the executable contract.

Do not repair these by adding another conflicting prompt. Fix the shared
exporter/installer contract or remove the unsupported declaration.
